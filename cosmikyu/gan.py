from cosmikyu import config
import numpy as np
import os

from torchvision.utils import save_image
from torch.autograd import Variable

import torch.nn as nn
import torch

import mlflow

class GAN(object):
    def __init__(self, identifier, shape, latent_dim, output_path=None, experiment_path=None, cuda=False, ngpu=1):
        self.cuda = cuda
        self.ngpu = 0 if not self.cuda else ngpu
        self.shape = shape
        self.latent_dim = latent_dim
        self.identifier = identifier
        
        self.output_path = output_path or os.path.join(config.default_output_dir)
        self.tracking_path = os.path.join(self.output_path, "mlruns")
        self.experiment_path = experiment_path or os.path.join(self.output_path, identifier)
        mlflow.set_tracking_uri(self.tracking_path)
        self.experiment = mlflow.get_experiment_by_name(identifier) or mlflow.create_experiment(identifier)

        if torch.cuda.is_available() and not self.cuda:
            print("[WARNING] You have a CUDA device. You probably want to run with CUDA enabled")
        self.device = torch.device("cuda:0" if self.cuda else "cpu")

        self.generator = None
        self.discriminator = None

    def load_states(self, output_path):
        generator_state_file = os.path.join(output_path, "generator.pt")
        discriminator_state_file = os.path.join(output_path, "discriminator.pt")

        try:
            print("loading saved states")
            self.generator.load_state_dict(torch.load(generator_state_file, map_location=self.device))
            self.discriminator.load_state_dict(torch.load(discriminator_state_file, map_location=self.device))
        except Exception:
            print("failed to load saved states")

    def save_states(self, output_path):
        print("saving states")
        generator_state_file = os.path.join(output_path, "generator.pt")
        discriminator_state_file = os.path.join(output_path, "discriminator.pt")
        torch.save(self.generator.state_dict(), generator_state_file)
        torch.save(self.discriminator.state_dict(), discriminator_state_file)

    def _get_optimizers(self, **kwargs):
        raise NotImplemented()        

    def _post_process_discriminator(self, **kwargs):
        pass

    def _eval_discriminator_loss(self, real_imgs, gen_imgs): 
        raise NotImplemented()
    
    def _eval_generator_loss(self, real_imgs, gen_imgs): 
        raise NotImplemented()
    
    def train(self, dataloader, nepochs=200, ncritics=5, sample_interval=1000,
              save_interval=10000, load_states=True, save_states=True, verbose=True, visdom_plotter=None, mlflow_run=None, **kwargs):
        kwargs.update({"nepochs": nepochs, "ncritics": ncritics})
        # Logging parameters
        if mlflow_run:
            for key, value in kwargs.items():
                mlflow.log_param(key, value)

        # Base Setup
        run_id = "trial" if not mlflow_run else mlflow_run.info.run_id
        run_path = os.path.join(self.experiment_path, run_id)
        
        os.makedirs(run_path, exist_ok=True) 
        if load_states:
            self.load_states(run_path)
        
        Tensor = torch.cuda.FloatTensor if self.cuda else torch.FloatTensor

        # Get Optimizers
        opt_gen, opt_disc = self._get_optimizers(**kwargs)

        batches_done = 0
        for epoch in range(nepochs):
            for i, sample in enumerate(dataloader):
                imgs = sample[0]
                real_imgs = Variable(imgs.type(Tensor))

                opt_disc.zero_grad()

                # Sample noise as generator input
                z = Variable(Tensor(np.random.normal(0, 1, (imgs.shape[0], self.latent_dim))))

                # Generate a batch of images
                gen_imgs = self.generator(z).detach()
                # Adversarial loss
                loss_D = self._eval_discriminator_loss(real_imgs, gen_imgs)
                loss_D.backward()
                opt_disc.step()

                # Hook for Discriminator Post Processing
                self._post_process_discriminator(**kwargs)

                if i % ncritics == 0:
                    opt_gen.zero_grad()

                    # Generate a batch of images
                    gen_imgs = self.generator(z)
                    # Adversarial loss\
                    loss_G = self._eval_generator_loss(real_imgs, gen_imgs)

                    loss_G.backward()
                    opt_gen.step()

                    if verbose:
                        print("[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f]"
                              % (epoch, nepochs, batches_done % len(dataloader), len(dataloader), -1 * loss_D.item(),
                                 -1 * loss_G.item())
                              )

                if batches_done % sample_interval == 0:
                    save_image(gen_imgs.data[:5], os.path.join(run_path, "%d.png" % batches_done), normalize=True)
                if batches_done % save_interval == 0 and save_states:
                    self.save_states(run_path)
                batches_done += 1

                if visdom_plotter is not None:
                    visdom_plotter.plot("D loss", 'D loss', np.array([batches_done]), np.array([-1 * loss_D.item()]),
                                        xlabel='batches_done')
                    visdom_plotter.plot("G loss", 'G_loss', np.array([batches_done]), np.array([-1 * loss_G.item()]),
                                        xlabel='batches_done')
        #self._end_train(save_states)
        if save_states:
            self.save_states(run_path)
    

        
class WGAN(GAN):
    def __init__(self, identifier, shape, latent_dim, output_path=None, experiment_path=None, cuda=False, ngpu=1):
        super().__init__(identifier, shape, latent_dim, output_path=output_path, experiment_path=experiment_path, cuda=cuda, ngpu=ngpu)

        self.generator = WGAN_Generator(shape, latent_dim, ngpu=self.ngpu).to(device=self.device)
        self.discriminator = WGAN_Discriminator(shape, ngpu=self.ngpu).to(device=self.device)

    def _post_process_discriminator(self, **kwargs):
        clip_tresh = kwargs["clip_tresh"]
        # Clip weights of discriminator
        for p in self.discriminator.parameters():
            p.data.clamp_(-clip_tresh, clip_tresh)     

    def _eval_discriminator_loss(self, real_imgs, gen_imgs):         
        return -torch.mean(self.discriminator(real_imgs)) + torch.mean(self.discriminator(gen_imgs))

    def _get_optimizers(self, **kwargs):
        opt_gen = torch.optim.RMSprop(self.generator.parameters(), lr=kwargs['lr'])
        opt_disc = torch.optim.RMSprop(self.discriminator.parameters(), lr=kwargs['lr'])
        return opt_gen, opt_disc

    def _eval_generator_loss(self, real_imgs, gen_imgs): 
        return  -torch.mean(self.discriminator(gen_imgs))
    
    def train(self, dataloader, nepochs=200, ncritics=5, sample_interval=1000,
              save_interval=10000, load_states=True, save_states=True, verbose=True, visdom_plotter=None, mlflow_run=None, lr=0.00005, clip_tresh=0.01):

        super().train(dataloader, nepochs=nepochs, ncritics=ncritics, sample_interval=sample_interval,
              save_interval=save_interval, load_states=load_states, save_states=save_states, verbose=verbose, visdom_plotter=visdom_plotter, mlflow_run=mlflow_run,
              lr=lr, clip_tresh=clip_tresh)

class WGAN_Generator(nn.Module):
    def __init__(self, shape, latent_dim, ngpu=1):
        super(WGAN_Generator, self).__init__()
        self.shape = shape
        self.latent_dim = latent_dim
        self.ngpu = ngpu

        def custom_layer(dim_in, dim_out, batch_normalize=True):
            layers = [nn.Linear(dim_in, dim_out)]
            if batch_normalize:
                layers.append(nn.BatchNorm1d(dim_out, 0.8))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            return layers

        self.model = nn.Sequential(
            *custom_layer(self.latent_dim, 128, batch_normalize=False),
            *custom_layer(128, 256),
            *custom_layer(256, 512),
            *custom_layer(512, 1024),
            nn.Linear(1024, int(np.prod(shape))),
            nn.Tanh()
        )

    def forward(self, z):
        if z.is_cuda and self.ngpu > 1:
            img = nn.parallel.data_parallel(self.model, z, range(self.ngpu))
        else:
            img = self.model(z)
        img = img.view(img.shape[0], *self.shape)
        return img


class WGAN_Discriminator(nn.Module):
    def __init__(self, shape, ngpu=1):
        super(WGAN_Discriminator, self).__init__()
        self.shape = shape
        self.ngpu = ngpu

        self.model = nn.Sequential(
            nn.Linear(int(np.prod(self.shape)), 512),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(256, 1)
        )

    def forward(self, img):
        flattened = img.view(img.shape[0], -1)
        if img.is_cuda and self.ngpu > 1:
            ret = nn.parallel.data_parallel(self.model, flattened, range(self.ngpu))
        else:
            ret = self.model(flattened)
        return ret
