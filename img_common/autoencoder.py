""" Implementation of image autoencoder and its dynamics. """

import torch.cuda
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import ImageFolder
from pathlib import Path
import numpy as np
from multiprocessing import Process, Manager, Pool
from psutil import cpu_count
import time
import sys
import pandas as pd
from itertools import repeat, product
from setproctitle import setproctitle
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter
import gc

from .generator import Generator
from .enums import *
from .one_cycle import *
from .processing import ImgProc
from traceback import print_exc

CURSOR_UP_ONE = '\x1b[1A'
ERASE_LINE = '\x1b[2K'


# TODO: calculate bpp
# TODO: add validation
# TODO: add learning rate schedules
# TODO: use pytorch multiprocessing
# TODO: add gain_net
class AutoEnc:
    """ Class representing the image autoencoders. It has all methods necessary
        to operate with them.
    """
    ckpt_file_pattern = "vloss{:.4g}.ckpt"

    class State:
        """ Class with useful information about the current execution of the
            autoencoder. It provides the information of the instantiated model
            currently running.
        """

        def __init__(self, exec_mode=ExecMode.TRAIN):
            self.global_step = []
            self.step = None
            self.autoenc_opt = []
            self.exec_mode = exec_mode
            self.out_type = None
            self.autoenc = []
            self.loss = None
            self.ckpt = None
            self.out_queue = None

    def __init__(self, autoencoder_conf, run_conf, queue_mem=0.1):
        self._queue_mem = queue_mem
        self.auto_cfg = autoencoder_conf.copy()
        self.run_cfg = run_conf.copy()
        self.st = None
        self.generators = self._instantiate_generators()
        out = Path(self.run_cfg['out_folder'])
        cnt = 0
        while out.exists():
            out = Path(self.run_cfg['out_folder'] + '_' + str(cnt))
            cnt += 1
        out.mkdir(parents=True, exist_ok=True)
        self.out_name = out
        self.ckpt_path_pattern = str(
            self.out_name / str(Folders.CHECKPOINTS) / self.ckpt_file_pattern)

    @staticmethod
    def _clear_last_lines(n=1):
        """ Clear the last n lines in stdout """
        for _ in range(n):
            sys.stdout.write(CURSOR_UP_ONE)
            sys.stdout.write(ERASE_LINE)

    def _instantiate_generators(self):
        """ Method to instantiate generator objects """
        shape = self.auto_cfg['input_shape']
        conf = self.run_cfg['generators']
        gen = {}

        if conf['train']['enabled']:
            gen['train'] = Generator(shape, conf['train'])
            gen['valid'] = Generator(shape, conf['valid'])
        if conf['test']['enabled']:
            gen['test'] = Generator(shape, conf['test'])
        return gen

    @staticmethod
    def update_lr(optimizer, lr):
        for g in optimizer.param_groups:
            g['lr'] = lr

    @staticmethod
    def update_mom(optimizer, mom):
        for g in optimizer.param_groups:
            g['momentum'] = mom

    def _create_model(self):
        """ This method creates all objects necessary for running a model. """
        st, conf = self.st, self.auto_cfg

        class AutoEncoder(nn.Module):
            def __init__(self):
                super(AutoEncoder, self).__init__()
                self.encoder = nn.Sequential(
                    nn.Conv2d(3, 256, 5, 2, 2),
                    nn.ReLU(),
                    nn.Conv2d(256, 128, 5, 2, 2)
                )
                self.decoder = nn.Sequential(
                    nn.ConvTranspose2d(128, 64, 2, 2),
                    nn.ReLU(),
                    nn.ConvTranspose2d(64, 256, 5, 2, 2, output_padding=1),
                    nn.ReLU(),
                    nn.ConvTranspose2d(256, 3, 1, 1),
                    nn.ReLU()
                )

            def forward(self, x):
                x = self.encoder(x)
                x = self.decoder(x)
                return x

        model = AutoEncoder()
        if torch.cuda.is_available():
            model = model.cuda()
        st.autoenc.append(model)
        optimizer = Optimizers(conf['lr_politics']['optimizer']).value
        learning_rate = conf['lr_politics']['lr']
        st.autoenc_opt.append(optimizer(learning_rate, model.parameters()))
        st.loss = Losses(conf['loss']).value

    @staticmethod
    def _timeout_msg(function, msg):
        """ Auxiliary function to the asynchronous functions to print
            a message of timeout
        """
        print('Timeout in', function, 'while processing', msg, end='\n\n')

    def _create_out_folder(self):
        """ Auxiliary function to _handle_output that creates the prediction
            folder
        """
        if self.st.out_type == OutputType.NONE:
            return None

        pred_folder = self.out_name
        if self.st.exec_mode == ExecMode.TRAIN:
            if self.st.out_type == OutputType.RESIDUES:
                pred_folder = str(OutputType.RESIDUES)
        if self.st.exec_mode == ExecMode.TEST:
            pred_folder /= str(Folders.TEST)
        else:
            pred_folder /= str(Folders.VALIDATION)
        pred_folder.mkdir(parents=True, exist_ok=True)

        return pred_folder

    @staticmethod
    def _save_out_analysis(img_paths, folder, bpps_proxy, metrics_proxy):
        """ Auxiliary function of _handle_output. It saves a csv containing
            the analysis for all images wrt all metrics for each codec.
        """
        def save_csv(data, csv_path, index, levels):
            names = ['bpp'] + list(map(lambda x: str(x), Metrics))
            cols = [x[0] + str(x[1]) for x in product(names, range(levels))]
            df = pd.DataFrame(data, index=pd.Index(index, name='img'),
                              columns=pd.Index(cols))
            df = df.sort_index()
            mean_df = pd.DataFrame(df.mean(axis=0).values.reshape(1, -1),
                                   columns=df.columns, index=pd.Index(['mean']))
            full_df = pd.concat((df, mean_df))
            full_df.to_csv(str(csv_path), float_format='%.5f')

        csv_path = list(map(
            lambda s: folder / ('_metrics_' + str(s) + '.csv'), Codecs))
        # dims: (codecs, images, levels)
        bpps = np.array(list(map(list, bpps_proxy)))
        levels = bpps.shape[-1]
        # dims: (codecs, metrics, images, levels)
        metrics = np.array(list(map(list, metrics_proxy.flat))).reshape(
            (*list(metrics_proxy.shape), len(bpps[0]), -1))
        # dims: (codecs, images, metrics, levels)
        metrics = metrics.swapaxes(1, 2)
        # merge metrics and levels to just one dimension
        metrics = metrics.reshape((*list(metrics.shape[:-2]), -1))
        data = np.concatenate((bpps, metrics), axis=2)
        list(map(save_csv, data, csv_path, repeat(img_paths),
                 repeat(levels)))

    @staticmethod
    def _codecs_out_routines(pools, path, img_num, bpps, metrics,
                             data, patches, out_folder):
        """ Auxiliary function of _handle_output. It does all routines necessary
            to the outputs and analysis of the codecs
        """
        pools[0].apply_async(ImgProc.calc_bpp_using_gzip,
                             (data, path, bpps[Codecs.NET], img_num))
        pools[1].apply_async(AutoEnc._save_imgs_from_patches,
                             (path, out_folder, patches, bpps[Codecs.NET],
                              metrics[Codecs.NET], img_num))

    @staticmethod
    def _set_proc_name_in_pool(string):
        """ Auxiliary function to name the pool of processes """
        setproctitle(string)

    @staticmethod
    def _instantiate_shared_variables(var_len):
        """ Auxiliary function that instantiate the variables maintained by
            the manager. It's used in _handle_output function
        """
        # positions: bpp, orig img, net, jpeg, jpeg2k, plots
        num_procs = np.array([.30, .45])
        num_procs = np.ceil(num_procs * cpu_count()).astype(int)
        names = list(map(
            lambda n: 'python3 - ' + n,
            ['calc_bpp_using_gzip', '_save_imgs_from_patches']))
        pools = [Pool(n_proc, AutoEnc._set_proc_name_in_pool, (name,))
                 for n_proc, name in zip(num_procs, names)]
        manager = Manager()
        n_codecs, n_metrics = len(Codecs), len(Metrics)
        bpps = np.empty((n_codecs,), dtype=object)
        bpps[:] = [manager.list([None] * var_len) for _ in range(n_codecs)]
        metrics = np.empty((n_codecs * n_metrics,), dtype=object)
        metrics[:] = [manager.list([None] * var_len)
                      for _ in range(n_codecs * n_metrics)]
        metrics = metrics.reshape((n_codecs, n_metrics))

        return pools, bpps, metrics

    def _handle_output(self):
        """ Routine executed to handle the output of the model """
        setproctitle('python3 - _handle_output')
        gen = self.generators[str(self.st.exec_mode)]
        out_folder = self._create_out_folder()
        img_pathnames = list(gen.get_db_files_pathnames())
        pools, bpps, metrics = self._instantiate_shared_variables(
            len(img_pathnames))

        for img_num, img in enumerate(img_pathnames):
            # TODO: the collector doesn't work if called later. The memory is
            #  not release by python. Investigate why. It would be good to call
            #  it less frequently.
            if img_num % 5 == 0:
                gc.collect()
            model_data = self.st.out_queue.get()
            patches = model_data
            AutoEnc._codecs_out_routines(pools, img, img_num, bpps, metrics,
                                         model_data, patches,
                                         out_folder)

        list(map(lambda p: p.close(), pools))
        list(map(lambda p: p.join(), pools))
        AutoEnc._save_out_analysis(img_pathnames, out_folder, bpps, metrics)

    @staticmethod
    def _save_imgs_from_patches(orig_path, save_folder, patches,
                                bpp_proxy, metrics_proxy, pos, color='RGB'):
        """ Function that gets the predicted patches, and reconstruct the image.
        """
        try:
            orig_ref = ImgProc.load_image(orig_path, ImgData.UBYTE, color)

            for metric in Metrics:
                metrics_proxy[metric[0]][pos] = ImgProc.calc_metric(
                    orig_ref, patches, metric)
            cont = 0
            while not bpp_proxy[pos]:
                time.sleep(1)
                if cont > 100:
                    AutoEnc._timeout_msg(AutoEnc._save_imgs_from_patches,
                                         orig_path)
                    return
            new_path = AutoEnc.get_out_pathname(orig_path, save_folder, '.png')
            ImgProc.save_img(patches, new_path, color)
        except Exception:
            print_exc()

    @staticmethod
    def _save_metric_plot(img_path, folder, metrics_proxy, bpp_proxy, pos,
                          metrics):
        """ Method that saves a scatter plot related to an image. """
        try:
            for curr_metric, curr_bpp in zip(metrics_proxy, bpp_proxy):
                cont = 0
                while not curr_metric[pos] or not curr_bpp[pos]:
                    time.sleep(1)
                    cont += 1
                    if cont > 100:
                        AutoEnc._timeout_msg(AutoEnc._save_metric_plot,
                                             img_path)
                        return

            curr_metric = list(map(lambda e: e[pos], metrics_proxy))
            curr_bpp = list(map(lambda e: e[pos][0], bpp_proxy))

            fig, ax = plt.subplots()
            plt.xlabel('bpp')
            plt.ylabel(str(metrics))
            plt.grid(True)
            for bpp, metric in zip(curr_bpp, curr_metric):
                plt.plot(bpp, metric, marker='o', markersize=6, linewidth=2)
            legend = list(map(lambda s: str(s), Codecs))
            plt.legend(legend, loc='upper left')
            img_size = ImgProc.get_size(img_path)
            plt.title(str(img_path.name) + ', {} x {}'.format(*img_size))

            min_bpp, max_bpp = np.amin(curr_bpp), np.amax(curr_bpp)
            min_metric, max_metric = np.amin(curr_metric), np.amax(curr_metric)
            plt.xticks(np.linspace(min_bpp, max_bpp, 20))
            plt.xticks(rotation=90)
            plt.yticks(np.linspace(min_metric, max_metric, 20))

            if min_bpp != max_bpp:
                plt.xlim(min_bpp, max_bpp)
            if min_metric != max_metric:
                plt.ylim(min_metric, max_metric)
            ax.xaxis.set_major_formatter(FormatStrFormatter('%.3f'))
            ax.yaxis.set_major_formatter(FormatStrFormatter('%.3f'))
            plt.tight_layout()
            plot_name = folder / (img_path.stem + '_plot_' +
                                  str(metrics) + Path(img_path.name).suffix)
            plt.savefig(str(plot_name), dpi=360)
            plt.close()
        except Exception:
            print_exc()

    @staticmethod
    def get_out_pathname(img_path, save_folder, ext='.png'):
        """ Construct an output pathname based on original image path """
        img_path = Path(img_path)
        save_folder = Path(save_folder)

        new_name = img_path.stem + ext
        save_name = save_folder / new_name
        return save_name

    def _run_model(self):
        """ Generic function that executes the current model based on the
            parameters passed
        """
        setproctitle('python3 - _run_model')
        st, conf, run = self.st, self.auto_cfg, self.run_cfg

        if st.exec_mode:
            st.out_queue = Manager().Queue(run['queue_size'])
            patch_proc = Process(target=AutoEnc._handle_output, args=(self,))
            patch_proc.start()

        img_transform = transforms.Compose([transforms.ToPILImage()])
        data_transforms = {
            'train': transforms.Compose([transforms.ToTensor()]),
            'valid': transforms.Compose([transforms.ToTensor()]),
            'test': transforms.Compose([transforms.ToTensor()])
        }
        images = {
            'train': ImageFolder(root=run['generators']
            ['train']['path'], transform=data_transforms['train']),
            'valid': ImageFolder(root=run['generators']
            ['valid']['path'], transform=data_transforms['valid']),
            'test': ImageFolder(root=run['generators']
            ['test']['path'], transform=data_transforms['test'])
        }
        data_loader = {
            'train': DataLoader(images['train'], batch_size=
            conf['input_shape'][0], num_workers=run['workers'],
                                shuffle=run['shuffle']),
            'valid': DataLoader(images['valid'], batch_size=
            conf['input_shape'][0], num_workers=run['workers']),
            'test': DataLoader(images['test'], batch_size=
            conf['input_shape'][0], num_workers=run['workers'])
        }
        # Execution of the model
        iter_str = '{:d}/' + str(
            len(data_loader[str(st.exec_mode).lower()])) + ': {}'
        onecycle = OneCycle(int(len(
            data_loader[str(st.exec_mode).lower()]) //
                                         conf['input_shape'][0]), 0.8,
                                         prcnt=(1 - 82) * 100,
                                         momentum_vals=(0.95, 0.8))

        for batch_idx, (data, _) in enumerate(
                data_loader[str(st.exec_mode).lower()]):
            if torch.cuda.is_available():
                data = data.cuda()
            if conf['lr_politics']['schedule'] == 'one_cycle' \
                    and st.exec_mode == ExecMode.TRAIN:
                lr, mom = onecycle.calc()
                for g in st.autoenc_opt[0].param_groups:
                    g['lr'] = lr
                for g in st.autoenc_opt[0].param_groups:
                    g['momentum'] = mom

            # ===================forward=====================
            # Prediction of the model
            output = st.autoenc[0](data)
            # ===================backward=====================
            # Backward pass: compute gradient of the loss with respect to all
            # the learnable parameters of the model.
            # Zero the gradients before running the backward pass
            st.autoenc_opt[0].zero_grad()
            # Compute loss
            loss = st.loss(output, data)
            # Update optimizer's parameters
            if st.exec_mode == ExecMode.TRAIN:
                loss.backward()
                st.autoenc_opt[0].step()
            print(iter_str.format(batch_idx+1, str(loss.item())))
            AutoEnc._clear_last_lines()
            if st.exec_mode:
                for j in range(output.size()[0]):
                    st.out_queue.put(np.array(img_transform
                                              (output.cpu().data[j])))
        if st.exec_mode:
            patch_proc.join()

    def test_model(self):
        """ Evaluate the eager model for validation or testing """
        if not self.st:
            self.st = self.State(exec_mode=ExecMode.TEST)
            self._create_model()
        self.st.epoch_str = ''
        self.st.exec_mode = ExecMode.TEST
        self.st.out_type = OutputType.RECONSTRUCTION

        print('\nTESTING:')
        self._run_model()

    # TODO: incorporate possibility of validation steps between iterations
    def train_model(self):
        """ Train the model using the eager execution """
        self.st = self.State()
        self._create_model()
        print('\nTRAINING:')
        self.st.exec_mode = ExecMode.TRAIN
        self.st.out_type = OutputType.NONE
        self._run_model()