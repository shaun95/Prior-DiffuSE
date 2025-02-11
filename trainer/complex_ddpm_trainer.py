import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from copy import deepcopy
import utils.params
from utils import *
from model import *
# from model.diff2 import DiffWave
from model.diff3 import DiffUNet1
from model.piror_grad import Nocon
import logging
import wandb
from tqdm import tqdm
from scripts.draw_spectrum import plot_stft
import matplotlib.pyplot as plt
import torch.nn.functional as F
# from plotnine import *
# import pandas as pd

wandb.init(project="ddpm")


class ComplexDDPMTrainer(object):
    def __init__(self, args, config):
        # self.maxd = 0 # 34.3
        # self.mind = 100 # 20
        # self.piror_max = 0  # 3.93
        # self.piror_min = 100 # -3.87
        # self.c = 1
        self.c = 11
        # self.c = 2/3
        # config
        self.args = deepcopy(args)
        self.config = deepcopy(config)
        self.params = utils.params.params
        self.pirorgrad = self.params.pirorgrad
        self.deltamu = self.params.deltamu
        self.step = 0
        self.loss_fn_eva = nn.L1Loss()      # use for evaluation of x0


        beta = np.array(self.params.noise_schedule)     # noise_schedule --> beta       noise_level --> alpha^bar
        noise_level = np.cumprod(1 - beta)
        self.noise_level = torch.tensor(noise_level.astype(np.float32))



        '''dataset & dataloader'''
        collate = Collate(self.config)
        self.tr_dataset = VBTrDataset('data/noisy_trainset_wav', 'data/clean_trainset_wav', config)
        cv_dataset = VBCvDataset('data/noisy_testset_wav', 'data/clean_testset_wav', config)
        logging.info(f'Total {self.tr_dataset.__len__()} train data.')  # 11572
        logging.info(f'Total {cv_dataset.__len__()} eval data.')  # 824
        self.tr_dataloader = DataLoader(self.tr_dataset,
                                        batch_size=self.config.train.batch_size,
                                        shuffle=True,
                                        drop_last=True,
                                        num_workers=os.cpu_count(),
                                        collate_fn=collate.collate_fn)
        self.cv_dataloader = DataLoader(cv_dataset,
                                        batch_size=self.config.train.batch_size,
                                        # batch_size=1,
                                        shuffle=True,
                                        drop_last=True,
                                        num_workers=os.cpu_count(),
                                        collate_fn=collate.collate_fn)

        '''model'''
        self.model = eval(self.config.model.name)().cuda()
        if self.pirorgrad:
            self.model_ddpm = DiffUNet1(self.params).cuda()
        elif self.deltamu:
            self.model_ddpm = Nocon(self.params).cuda()
        else:
            self.model_ddpm = DiffUNet1(self.params).cuda()

        '''optimizer'''
        if self.config.optim.optimizer == 'Adam':
            self.optimizer = torch.optim.Adam(
                self.model.parameters(),
                self.config.optim.lr,
                weight_decay=self.config.optim.l2
            )
            self.optimizer_ddpm = torch.optim.Adam(
                self.model_ddpm.parameters(),
                self.config.optim_ddpm.lr,
                weight_decay=self.config.optim_ddpm.l2
            )

        '''retrain'''
        if self.args.retrain:
            pretrained_data = torch.load(os.path.join(self.args.checkpoint, 'best_checkpoint.pth'))
            self.model.load_state_dict(pretrained_data[0])
            self.optimizer.load_state_dict(pretrained_data[1])
            if self.args.draw or self.args.joint:
                self.model_ddpm.load_state_dict(pretrained_data[2])
                self.optimizer_ddpm.load_state_dict(pretrained_data[3])

        '''logger'''
        wandb.watch(self.model, log="all")




    def inference_schedule(self, fast_sampling=False):
        """
        Compute fixed parameters in ddpm

        :return:
            alpha:          alpha for training,         sizelike noise_schedule
            beta:           beta for inference,         sizelike inference_noise_schedule or noise_schedule
            alpha_cum:      alpha_cum for inference
            sigmas:         sqrt(beta_t^tilde)
            T:              Timesteps
        """
        training_noise_schedule = np.array(self.params.noise_schedule)
        inference_noise_schedule = np.array(
            self.params.inference_noise_schedule) if fast_sampling else training_noise_schedule

        talpha = 1 - training_noise_schedule    # alpha_t for train
        talpha_cum = np.cumprod(talpha)

        beta = inference_noise_schedule
        alpha = 1 - beta
        alpha_cum = np.cumprod(alpha)
        sigmas = [0 for i in alpha]
        for n in range(len(alpha) - 1, -1, -1):
            sigmas[n] = ((1.0 - alpha_cum[n - 1]) / (1.0 - alpha_cum[n]) * beta[n]) ** 0.5      # sqrt(beta_t^tilde)
        # print("sigmas", sigmas)

        T = []
        for s in range(len(inference_noise_schedule)):
            for t in range(len(training_noise_schedule) - 1):
                if talpha_cum[t + 1] <= alpha_cum[s] <= talpha_cum[t]:
                    twiddle = (talpha_cum[t] ** 0.5 - alpha_cum[s] ** 0.5) / (
                                talpha_cum[t] ** 0.5 - talpha_cum[t + 1] ** 0.5)
                    T.append(t + twiddle)   # from schedule to T which as the input of model
                    break
        T = np.array(T, dtype=np.float32)

        # idx = [i for i in range(len(alpha))]
        # v_dict = {
        #     "idx": idx,
        #     "alpha": alpha,
        #     "beta": beta,
        #     "alpha_cum": alpha_cum,
        #     "sigmas": sigmas,
        #     "T": T
        # }
        # data_df = pd.DataFrame(v_dict)
        # data_plot = pd.melt(data_df[["idx", "alpha","beta", "alpha_cum", "sigmas"]], id_vars=["idx"])   # melt turn col_name into feature
        # print(data_plot)
        # plot =  ggplot(data_plot) + geom_line(aes(x="idx", y="value", color='variable'))+ xlab("steps")
        # plot.save("fast_sample_diffwave.pdf")

        return alpha, beta, alpha_cum, sigmas, T

    def draw_audio(self):
        self.model.eval()
        self.model_ddpm.eval()
        alpha, beta, alpha_cum, sigmas, T = self.inference_schedule(fast_sampling=self.params.fast_sampling)

        with torch.no_grad():
            for batch in tqdm(self.cv_dataloader):
                batch_feat = batch.feats.cuda()
                batch_label = batch.labels.cuda()
                # print(batch_label)
                '''four approaches for feature compression'''
                noisy_phase = torch.atan2(batch_feat[:, -1, :, :], batch_feat[:, 0, :, :])  # [B, 1, T, F]
                clean_phase = torch.atan2(batch_label[:, -1, :, :], batch_label[:, 0, :, :])

                if self.config.train.feat_type == 'normal':
                    batch_feat, batch_label = torch.norm(batch_feat, dim=1), torch.norm(batch_label, dim=1)
                elif self.config.train.feat_type == 'sqrt':
                    batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.5, (
                        torch.norm(batch_label, dim=1)) ** 0.5
                elif self.config.train.feat_type == 'cubic':
                    batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.3, (
                        torch.norm(batch_label, dim=1)) ** 0.3
                elif self.config.train.feat_type == 'log_1x':
                    batch_feat, batch_label = torch.log(torch.norm(batch_feat, dim=1) + 1), \
                                              torch.log(torch.norm(batch_label, dim=1) + 1)
                if self.config.train.feat_type in ['normal', 'sqrt', 'cubic', 'log_1x']:
                    batch_feat = torch.stack(
                        (batch_feat * torch.cos(noisy_phase), batch_feat * torch.sin(noisy_phase)),
                        # [B, 2, T, F]
                        dim=1)
                    batch_label = torch.stack(
                        (batch_label * torch.cos(clean_phase), batch_label * torch.sin(clean_phase)),
                        dim=1)

                '''evaluation'''
                init_audio = self.model(batch_feat)  # [B, 2, T, F]
                init_audio /= self.c
                batch_feat /= self.c
                # print( batch_label[0][0]/self.c)
                # print( init_audio[0][0])    # 矩阵最后几行值相同且与label不同
                # exit()
                if self.deltamu:
                    audio = torch.randn_like(init_audio) + init_audio
                else:
                    audio = torch.randn_like(init_audio)  # XT = N(0, I),  [N, 2, T, F]
                if self.args.sigma:
                    tmp = torch.flatten(torch.abs(init_audio), start_dim=2)
                    tmp /= torch.max(tmp, dim=2, keepdim=True).values
                    tmp = tmp / 2 + 0.5
                    mask = tmp.view(batch_label.shape)
                    audio = audio * (mask ** 0.5)
                N = audio.shape[0]
                gamma = [0 for i in alpha]  # the first 2 num didn't use
                for n in range(len(alpha)):
                    gamma[n] = sigmas[n]  # beta^tilde
                # print(gamma)    #[0.715, 0.0095, 0.031, 0.095, 0.220, 0.412]
                gamma[0] = 0.2  # beta^tilde_0 = 0.2
                # print("gamma",gamma)
                for n in range(len(alpha) - 1, -1, -1):
                    c1 = 1 / alpha[n] ** 0.5  # c1 in mu equation
                    c2 = beta[n] / (1 - alpha_cum[n]) ** 0.5  # c2 in mu equation
                    if self.pirorgrad:
                        predicted_noise = self.model_ddpm(audio, init_audio,
                                                          torch.tensor([T[n]], device=audio.device).repeat(N))
                    elif self.deltamu:
                        predicted_noise = self.model_ddpm(audio, torch.tensor([T[n]], device=audio.device).repeat(N))
                    else:
                        predicted_noise = self.model_ddpm(audio, batch_feat,  # z_theta(x_t, condition, t)
                                                          torch.tensor([T[n]], device=audio.device).repeat(N))

                    audio = c1 * (audio - c2 * predicted_noise)  # mu_theta(x_t, z_theta)
                    if n > 0:
                        noise = torch.randn_like(audio)
                        sigma = gamma[n]  # sigma = gamma[n]
                        newsigma = max(0, sigma - c1 * gamma[n])  # ???
                        if self.args.sigma:
                            noise = noise * (mask ** 0.5)
                        audio += newsigma * noise  # x_t-1 = mu_theta + beta^tilde * epsilon

                if self.pirorgrad:
                    audio += init_audio
                audio *= self.c
                init_audio *= self.c
                '''plot batch_label, init_audio, predicted'''
                # for i in range(N):
                #     print("________________________________________________________________")
                #     print("batch_label", torch.max(batch_label[i][0]/self.c), torch.min(batch_label[i][0]/self.c))
                #     # print("________________________________________________________________")
                #     print("init_audio", torch.max(init_audio[i][0]/self.c), torch.min(init_audio[i][0]/self.c))
                #     # print("________________________________________________________________")
                #     print("true-delta", torch.max(batch_label[i][0]/self.c-init_audio[i][0]/self.c), torch.min(batch_label[i][0]/self.c-init_audio[i][0]/self.c))
                #     # print("________________________________________________________________")
                #     print("predicted-delta", torch.max(audio[i][0]/self.c-init_audio[i][0]/self.c), torch.min(audio[i][0]/self.c-init_audio[i][0]/self.c))
                #     print("________________________________________________________________")
                #     fig, ax = plt.subplots(1,6, constrained_layout=True, figsize=(20,8), dpi=300)
                #
                #     pcm = ax[0].matshow((batch_feat[i][0] / self.c).cpu().numpy())
                #     ax[0].set_title("noisy_audio_" + str(i))
                #     fig.colorbar(pcm, ax=ax[0], shrink=0.6)
                #
                #     pcm = ax[1].matshow((batch_label[i][0]/self.c).cpu().numpy())
                #     ax[1].set_title("label_audio_" + str(i))
                #     fig.colorbar(pcm, ax=ax[1], shrink=0.6)
                #
                #     pcm = ax[2].matshow((init_audio[i][0]/self.c).cpu().numpy())
                #     ax[2].set_title("init_audio_" + str(i))
                #     fig.colorbar(pcm, ax=ax[2], shrink=0.6)
                #
                #     pcm = ax[3].matshow((audio[i][0]/ self.c).cpu().numpy())
                #     ax[3].set_title("predicted_audio_" + str(i))
                #     fig.colorbar(pcm, ax=ax[3], shrink=0.6)
                #
                #     pcm = ax[4].matshow((batch_label[i][0]/self.c).cpu().numpy() - (init_audio[i][0]/self.c).cpu().numpy())
                #     ax[4].set_title("true_delta_" + str(i))
                #     fig.colorbar(pcm, ax=ax[4], shrink=0.6)
                #
                #     pcm = ax[5].matshow((audio[i][0]/self.c).cpu().numpy() - (init_audio[i][0]/self.c).cpu().numpy())
                #     ax[5].set_title("predicted_delta_" + str(i))
                #     fig.colorbar(pcm, ax=ax[5], shrink=0.6)
                #
                #     fig.savefig("audio"+str(i))
                # exit()
                '''output wav'''
                esti_list = audio
                label_list = batch_label
                esti_mag, esti_phase = torch.norm(esti_list, dim=1), torch.atan2(esti_list[:, -1, :, :],
                                                                                 esti_list[:, 0, :, :])
                label_mag, label_phase = torch.norm(label_list, dim=1), torch.atan2(label_list[:, -1, :, :],
                                                                                    label_list[:, 0, :, :])
                if feat_type == 'sqrt':
                    esti_mag = esti_mag ** 2
                    esti_com = torch.stack((esti_mag * torch.cos(esti_phase), esti_mag * torch.sin(esti_phase)), dim=1)
                    label_mag = label_mag ** 2
                    label_com = torch.stack((label_mag * torch.cos(label_phase), label_mag * torch.sin(label_phase)),
                                            dim=1)
                elif feat_type == 'cubic':
                    esti_mag = esti_mag ** (10 / 3)
                    esti_com = torch.stack((esti_mag * torch.cos(esti_phase), esti_mag * torch.sin(esti_phase)), dim=1)
                    label_mag = label_mag ** (10 / 3)
                    label_com = torch.stack((label_mag * torch.cos(label_phase), label_mag * torch.sin(label_phase)),
                                            dim=1)
                elif feat_type == 'log_1x':
                    esti_mag = torch.exp(esti_mag) - 1
                    esti_com = torch.stack((esti_mag * torch.cos(esti_phase), esti_mag * torch.sin(esti_phase)), dim=1)
                    label_mag = torch.exp(label_mag) - 1
                    label_com = torch.stack((label_mag * torch.cos(label_phase), label_mag * torch.sin(label_phase)),
                                            dim=1)
                else:
                    esti_com = esti_list
                    label_com = label_list
                clean_utts, esti_utts = [], []
                utt_num = label_list.size()[0]
                for i in range(utt_num):
                    # print("utt_num: ", i)
                    tf_esti = esti_com[i, :, :, :].unsqueeze(dim=0).permute(0, 3, 2, 1).cpu()
                    t_esti = torch.istft(tf_esti, n_fft=320, hop_length=160, win_length=320,
                                         window=torch.hann_window(320)).transpose(1, 0).squeeze(dim=-1).numpy()
                    tf_label = label_com[i, :, :, :].unsqueeze(dim=0).permute(0, 3, 2, 1).cpu()
                    t_label = torch.istft(tf_label, n_fft=320, hop_length=160, win_length=320,
                                          window=torch.hann_window(320)).transpose(1, 0).squeeze(dim=-1).numpy()
                    t_len = (frame_list[i] - 1) * 160
                    t_esti, t_label = t_esti[:t_len], t_label[:t_len]
                    esti_utts.append(t_esti)
                    clean_utts.append(t_label)



                '''metrics compute'''
                # x
                batch_loss = com_mse_loss(audio, batch_label, batch.frame_num_list)
                batch_result = compare_complex(audio, batch_label, batch.frame_num_list,
                                               feat_type=self.config.train.feat_type)  # compute evaluate metrics
                # print(batch_result)
                all_loss_list.append(batch_loss.item())
                all_csig_list.append(batch_result[0])
                all_cbak_list.append(batch_result[1])
                all_covl_list.append(batch_result[2])
                all_pesq_list.append(batch_result[3])
                all_ssnr_list.append(batch_result[4])
                all_stoi_list.append(batch_result[5])

                # x_init
                # batch_loss_init = com_mse_loss(init_audio, batch_label, batch.frame_num_list)
                # batch_result = compare_complex(init_audio, batch_label, batch.frame_num_list,
                #                                feat_type=self.config.train.feat_type)  # compute evaluate metrics
                # all_loss_list_init.append(batch_loss_init.item())
                # all_csig_list_init.append(batch_result[0])
                # all_cbak_list_init.append(batch_result[1])
                # all_covl_list_init.append(batch_result[2])
                # all_pesq_list_init.append(batch_result[3])
                # all_ssnr_list_init.append(batch_result[4])
                # all_stoi_list_init.append(batch_result[5])

            wandb.log(
                {
                    'test_com_mse_loss': np.mean(all_loss_list),  # mean loss in val dataset
                    'test_mean_csig': np.mean(all_csig_list),
                    'test_mean_cbak': np.mean(all_cbak_list),
                    'test_mean_covl': np.mean(all_covl_list),
                    'test_mean_pesq': np.mean(all_pesq_list),
                    'test_mean_ssnr': np.mean(all_ssnr_list),
                    'test_mean_stoi': np.mean(all_stoi_list),
                    # 'test_mean_mse_loss_init': np.mean(all_loss_list_init),
                    # 'test_mean_csig_init': np.mean(all_csig_list_init),
                    # 'test_mean_cbak_init': np.mean(all_cbak_list_init),
                    # 'test_mean_covl_init': np.mean(all_covl_list_init),
                    # 'test_mean_pesq_init': np.mean(all_pesq_list_init),
                    # 'test_mean_ssnr_init': np.mean(all_ssnr_list_init),
                    # 'test_mean_stoi_init': np.mean(all_stoi_list_init)
                }
            )
    def train_ddpm(self, max_steps=None):
        torch.backends.cudnn.enabled = True

        '''variants initialize'''
        prev_cv_loss = float("inf")
        best_cv_loss = float("inf")
        cv_no_impv = 0
        harving = False

        '''draw from best ckp'''
        if self.args.draw:
            self.draw_audio()
            exit()

        '''training'''
        for epoch in range(self.config.train.n_epochs):
            logging.info(f'Epoch {epoch}')

            if self.args.eval is False:
                self.model_ddpm.train()
                self.model.train()
                for features in tqdm(self.tr_dataloader):
                    self.model_ddpm.train()
                    self.model.train()
                    if max_steps is not None and self.step >= max_steps:
                        return
                    loss = self.train_step(features)
                    self.step += 1
                    if torch.isnan(loss).any():
                        raise RuntimeError(f'Detected NaN loss at step {self.step}.')
            '''evaluation after an epoch'''
            self.model.eval()
            self.model_ddpm.eval()
            all_loss_list = []
            all_csig_list, all_cbak_list, all_covl_list, all_pesq_list, all_ssnr_list, all_stoi_list = [], [], [], [], [], []
            # all_loss_list_init = []
            # all_csig_list_init, all_cbak_list_init, all_covl_list_init, all_pesq_list_init, all_ssnr_list_init, all_stoi_list_init = [], [], [], [], [], []
            alpha, beta, alpha_cum, sigmas, T = self.inference_schedule(fast_sampling=self.params.fast_sampling)

            with torch.no_grad():
                for batch in tqdm(self.cv_dataloader):
                    batch_feat = batch.feats.cuda()
                    batch_label = batch.labels.cuda()
                    # print(batch_label)
                    '''four approaches for feature compression'''
                    noisy_phase = torch.atan2(batch_feat[:, -1, :, :], batch_feat[:, 0, :, :])  # [B, 1, T, F]
                    clean_phase = torch.atan2(batch_label[:, -1, :, :], batch_label[:, 0, :, :])

                    if self.config.train.feat_type == 'normal':
                        batch_feat, batch_label = torch.norm(batch_feat, dim=1), torch.norm(batch_label, dim=1)
                    elif self.config.train.feat_type == 'sqrt':
                        batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.5, (
                            torch.norm(batch_label, dim=1)) ** 0.5
                    elif self.config.train.feat_type == 'cubic':
                        batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.3, (
                            torch.norm(batch_label, dim=1)) ** 0.3
                    elif self.config.train.feat_type == 'log_1x':
                        batch_feat, batch_label = torch.log(torch.norm(batch_feat, dim=1) + 1), \
                                                  torch.log(torch.norm(batch_label, dim=1) + 1)
                    if self.config.train.feat_type in ['normal', 'sqrt', 'cubic', 'log_1x']:
                        batch_feat = torch.stack(
                            (batch_feat * torch.cos(noisy_phase), batch_feat * torch.sin(noisy_phase)),
                            # [B, 2, T, F]
                            dim=1)
                        batch_label = torch.stack(
                            (batch_label * torch.cos(clean_phase), batch_label * torch.sin(clean_phase)),
                            dim=1)


                    '''evaluation'''
                    init_audio = self.model(batch_feat)  # [B, 2, T, F]
                    init_audio /= self.c
                    batch_feat /= self.c
                    # print( batch_label[0][0]/self.c)
                    # print( init_audio[0][0])    # 矩阵最后几行值相同且与label不同
                    # exit()
                    if self.deltamu:
                        audio = torch.randn_like(init_audio) + init_audio
                    else:
                        audio = torch.randn_like(init_audio)                                                        # XT = N(0, I),  [N, 2, T, F]
                    if self.args.sigma:
                        tmp = torch.flatten(torch.abs(init_audio), start_dim=2)
                        tmp /= torch.max(tmp, dim=2, keepdim=True).values
                        tmp = tmp / 2 + 0.5
                        mask = tmp.view(batch_label.shape)
                        audio = audio * (mask ** 0.5)
                    N = audio.shape[0]
                    gamma = [0 for i in alpha]                                                                  # the first 2 num didn't use
                    for n in range(len(alpha)):
                        gamma[n] = sigmas[n]                                                                    # beta^tilde
                    # print(gamma)    #[0.715, 0.0095, 0.031, 0.095, 0.220, 0.412]
                    gamma[0] = 0.2                                                                              # beta^tilde_0 = 0.2
                    # print("gamma",gamma)
                    for n in range(len(alpha) - 1, -1, -1):
                        c1 = 1 / alpha[n] ** 0.5                                                                # c1 in mu equation
                        c2 = beta[n] / (1 - alpha_cum[n]) ** 0.5                                                # c2 in mu equation
                        if self.pirorgrad:
                            predicted_noise = self.model_ddpm(audio, init_audio, torch.tensor([T[n]], device=audio.device).repeat(N))
                        elif self.deltamu:
                            predicted_noise = self.model_ddpm(audio, torch.tensor([T[n]], device=audio.device).repeat(N))
                        else:
                            predicted_noise = self.model_ddpm(audio, batch_feat,                                    # z_theta(x_t, condition, t)
                                                          torch.tensor([T[n]], device=audio.device).repeat(N))
                        # print("predicted_noise", predicted_noise.shape)
                        # audio = c1 * ((1-gamma[n])*mu+gamma[n]* (noisy_audio-init_audio))  # 插值
                        audio = c1 * (audio - c2 * predicted_noise)                                             # mu_theta(x_t, z_theta)
                        # print("________________________________________________________________")
                        # print(predicted_noise)
                        # print("________________________________________________________________")
                        # print("predicted_noise", predicted_noise.shape)
                        # print("c1", c1)
                        # print("c2", c2)
                        # print("predicted_noise", predicted_noise)
                        # print(audio)
                        if n > 0:
                            noise = torch.randn_like(audio)
                            sigma = gamma[n]                                                                    # sigma = gamma[n]
                            newsigma = max(0, sigma - c1 * gamma[n])                                            # ???
                            if self.args.sigma:
                                noise = noise * (mask ** 0.5)
                            audio += newsigma * noise                                                           # x_t-1 = mu_theta + beta^tilde * epsilon

                        # audio = torch.clamp(audio, -35/11, 35/11) # Diffuse/ILVR used after preprocess
                    if self.pirorgrad:
                        audio += init_audio
                    audio *= self.c
                    init_audio *= self.c
                    '''plot batch_label, init_audio, predicted'''
                    # for i in range(N):
                    #     print("________________________________________________________________")
                    #     print("batch_label", torch.max(batch_label[i][0]/self.c), torch.min(batch_label[i][0]/self.c))
                    #     # print("________________________________________________________________")
                    #     print("init_audio", torch.max(init_audio[i][0]/self.c), torch.min(init_audio[i][0]/self.c))
                    #     # print("________________________________________________________________")
                    #     print("true-delta", torch.max(batch_label[i][0]/self.c-init_audio[i][0]/self.c), torch.min(batch_label[i][0]/self.c-init_audio[i][0]/self.c))
                    #     # print("________________________________________________________________")
                    #     print("predicted-delta", torch.max(audio[i][0]/self.c-init_audio[i][0]/self.c), torch.min(audio[i][0]/self.c-init_audio[i][0]/self.c))
                    #     print("________________________________________________________________")
                    #     fig, ax = plt.subplots(1,6, constrained_layout=True, figsize=(20,8), dpi=300)
                    #
                    #     pcm = ax[0].matshow((batch_feat[i][0] / self.c).cpu().numpy())
                    #     ax[0].set_title("noisy_audio_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[0], shrink=0.6)
                    #
                    #     pcm = ax[1].matshow((batch_label[i][0]/self.c).cpu().numpy())
                    #     ax[1].set_title("label_audio_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[1], shrink=0.6)
                    #
                    #     pcm = ax[2].matshow((init_audio[i][0]/self.c).cpu().numpy())
                    #     ax[2].set_title("init_audio_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[2], shrink=0.6)
                    #
                    #     pcm = ax[3].matshow((audio[i][0]/ self.c).cpu().numpy())
                    #     ax[3].set_title("predicted_audio_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[3], shrink=0.6)
                    #
                    #     pcm = ax[4].matshow((batch_label[i][0]/self.c).cpu().numpy() - (init_audio[i][0]/self.c).cpu().numpy())
                    #     ax[4].set_title("true_delta_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[4], shrink=0.6)
                    #
                    #     pcm = ax[5].matshow((audio[i][0]/self.c).cpu().numpy() - (init_audio[i][0]/self.c).cpu().numpy())
                    #     ax[5].set_title("predicted_delta_" + str(i))
                    #     fig.colorbar(pcm, ax=ax[5], shrink=0.6)
                    #
                    #     fig.savefig("audio"+str(i))
                    # exit()
                    '''metrics compute'''
                    # x
                    batch_loss = com_mse_loss(audio, batch_label ,batch.frame_num_list)
                    batch_result = compare_complex(audio, batch_label, batch.frame_num_list,
                                                   feat_type=self.config.train.feat_type)  # compute evaluate metrics
                    # print(batch_result)
                    all_loss_list.append(batch_loss.item())
                    all_csig_list.append(batch_result[0])
                    all_cbak_list.append(batch_result[1])
                    all_covl_list.append(batch_result[2])
                    all_pesq_list.append(batch_result[3])
                    all_ssnr_list.append(batch_result[4])
                    all_stoi_list.append(batch_result[5])

                    # x_init
                    # batch_loss_init = com_mse_loss(init_audio, batch_label, batch.frame_num_list)
                    # batch_result = compare_complex(init_audio, batch_label, batch.frame_num_list,
                    #                                feat_type=self.config.train.feat_type)  # compute evaluate metrics
                    # all_loss_list_init.append(batch_loss_init.item())
                    # all_csig_list_init.append(batch_result[0])
                    # all_cbak_list_init.append(batch_result[1])
                    # all_covl_list_init.append(batch_result[2])
                    # all_pesq_list_init.append(batch_result[3])
                    # all_ssnr_list_init.append(batch_result[4])
                    # all_stoi_list_init.append(batch_result[5])

                wandb.log(
                    {
                        'test_com_mse_loss': np.mean(all_loss_list),  # mean loss in val dataset
                        'test_mean_csig': np.mean(all_csig_list),
                        'test_mean_cbak': np.mean(all_cbak_list),
                        'test_mean_covl': np.mean(all_covl_list),
                        'test_mean_pesq': np.mean(all_pesq_list),
                        'test_mean_ssnr': np.mean(all_ssnr_list),
                        'test_mean_stoi': np.mean(all_stoi_list),
                        # 'test_mean_mse_loss_init': np.mean(all_loss_list_init),
                        # 'test_mean_csig_init': np.mean(all_csig_list_init),
                        # 'test_mean_cbak_init': np.mean(all_cbak_list_init),
                        # 'test_mean_covl_init': np.mean(all_covl_list_init),
                        # 'test_mean_pesq_init': np.mean(all_pesq_list_init),
                        # 'test_mean_ssnr_init': np.mean(all_ssnr_list_init),
                        # 'test_mean_stoi_init': np.mean(all_stoi_list_init)
                    }
                )
            if self.args.eval:
                exit()
            cv_loss = np.mean(all_loss_list)
            '''Adjust the learning rate and early stop'''
            if self.config.optim.half_lr > 1:
                if cv_loss >= prev_cv_loss:
                    cv_no_impv += 1
                    if cv_no_impv == self.config.optim.half_lr:  # adjust lr depend on cv_no_impv
                        harving = True
                    if cv_no_impv >= self.config.optim.early_stop > 0:  # early stop
                        logging.info("No improvement and apply early stop")
                        break
                else:
                    cv_no_impv = 0

            if harving == True:
                # dis model
                optim_state = self.optimizer.state_dict()
                for i in range(len(optim_state['param_groups'])):
                    optim_state['param_groups'][i]['lr'] = optim_state['param_groups'][i]['lr'] / 2.0
                self.optimizer.load_state_dict(optim_state)
                logging.info('Learning rate of dis model adjusted to %5f' % (optim_state['param_groups'][0]['lr']))

                # ddpm
                optim_state = self.optimizer_ddpm.state_dict()
                for i in range(len(optim_state['param_groups'])):
                    optim_state['param_groups'][i]['lr'] = optim_state['param_groups'][i]['lr'] / 2.0
                self.optimizer_ddpm.load_state_dict(optim_state)
                logging.info('Learning rate of ddpm adjusted to %5f' % (optim_state['param_groups'][0]['lr']))

                harving = False
            prev_cv_loss = cv_loss

            if cv_loss < best_cv_loss:
                logging.info(
                    f"last best loss is: {best_cv_loss}, current loss is: {cv_loss}, save best_checkpoint.pth")
                best_cv_loss = cv_loss
                states = [
                    self.model.state_dict(),
                    self.optimizer.state_dict(),
                    self.model_ddpm.state_dict(),
                    self.optimizer_ddpm.state_dict()
                ]
                torch.save(states, os.path.join(self.args.checkpoint, 'best_checkpoint.pth'))

            '''save latest checkpoint'''
            states = [
                self.model.state_dict(),
                self.optimizer.state_dict(),
                self.model_ddpm.state_dict(),
                self.optimizer_ddpm.state_dict()
            ]
            torch.save(states, os.path.join(self.args.checkpoint, f'checkpoint_{epoch}.pth'))

    def train_step(self, features):
        self.optimizer_ddpm.zero_grad()
        self.optimizer.zero_grad()
        batch_feat = features.feats.cuda()
        batch_label = features.labels.cuda()
        batch_frame_num_list = features.frame_num_list

        '''four approaches for feature compression'''
        noisy_phase = torch.atan2(batch_feat[:, -1, :, :], batch_feat[:, 0, :,
                                                           :])  # [B, 1, T, F] noisy_phase means <相成分>, batch_feat means <batch feature> ?
        clean_phase = torch.atan2(batch_label[:, -1, :, :],
                                  batch_label[:, 0, :, :])  # torch.atan2 means 双变量反正切函数,值域为（-pi, pi）
        if self.config.train.feat_type == 'normal':
            batch_feat, batch_label = torch.norm(batch_feat, dim=1), torch.norm(batch_label,
                                                                                dim=1)  # [B, 1, T, F] <相应频率下的分量幅度>
        elif self.config.train.feat_type == 'sqrt':
            batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.5, (
                torch.norm(batch_label, dim=1)) ** 0.5
        elif self.config.train.feat_type == 'cubic':
            batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.3, (
                torch.norm(batch_label, dim=1)) ** 0.3
        elif self.config.train.feat_type == 'log_1x':
            batch_feat, batch_label = torch.log(torch.norm(batch_feat, dim=1) + 1), \
                                      torch.log(torch.norm(batch_label, dim=1) + 1)
        if self.config.train.feat_type in ['normal', 'sqrt', 'cubic', 'log_1x']:
            batch_feat = torch.stack((batch_feat * torch.cos(noisy_phase), batch_feat * torch.sin(noisy_phase)),
                                     # [B, 2, T, F] <相应频率下的分量幅度在 实轴和虚轴的投影>
                                     dim=1)
            batch_label = torch.stack(
                (batch_label * torch.cos(clean_phase), batch_label * torch.sin(clean_phase)),
                dim=1)
        # print("batch_feat: ", batch_feat.shape)
        '''discriminative model'''
        if self.args.joint:
            init_audio_ = self.model(batch_feat) # [8, 2, 301, 161]
            loss_dis = eval(self.config.train.loss)(init_audio_, batch_label, batch_frame_num_list)
            init_audio = self.model(batch_feat).detach()
        else:
            init_audio = self.model(batch_feat).detach() # [8, 2, 301, 161]
            loss_dis = torch.tensor(0)
        # 计算 model 参数量 model 总参数数量和：1662565 model_ddpm 总参数数量和：1258371
        # params = list(self.model.parameters())
        # k = 0
        # for i in params:
        #     l = 1
        #     print("该层的结构：" + str(list(i.size())))
        #     for j in i.size():
        #         l *= j
        #     print("该层参数和：" + str(l))
        #     k = k + l
        # print("model 总参数数量和：" + str(k))
        # params = list(self.model_ddpm.parameters())
        # k = 0
        # for i in params:
        #     l = 1
        #     print("该层的结构：" + str(list(i.size())))
        #     for j in i.size():
        #         l *= j
        #     print("该层参数和：" + str(l))
        #     k = k + l
        # print("model_ddpm 总参数数量和：" + str(k))
        # exit(0)



        '''ddpm'''
        batch_label /= self.c
        init_audio /= self.c
        # batch_feat /= self.c
        N = batch_label.shape[0]  # Batch size

        device = batch_label.device
        self.noise_level = self.noise_level.to(device)                                  # alpha_bar     [1000]

        t = torch.randint(0, len(self.params.noise_schedule), [N], device=device)
        # print("t:", t)
        noise_scale = self.noise_level[t].unsqueeze(1).unsqueeze(2).unsqueeze(3)        # alpha_bar_t       [N, 1, 1, 1]
        noise_scale_sqrt = noise_scale ** 0.5                                           # sqrt(alpha_bar_t) [N, 1, 1, 1]
        noise = torch.randn_like(batch_label)                                            # epsilon           [N, 2, T, F]
        if self.args.sigma:
            tmp = torch.flatten(torch.abs(init_audio), start_dim=2)
            tmp /= torch.max(tmp, dim=2, keepdim=True).values
            tmp = tmp / 2 + 0.5
            mask = tmp.view(batch_label.shape)
            noise = noise * mask **0.5
            # print(torch.max(mask), torch.min(mask))
            # exit()
        if self.pirorgrad:
            # for i in range(N):
            #     plt.matshow((batch_label-init_audio)[i][0].cpu().numpy())
            #     plt.colorbar()
            #     plt.savefig("batch_label-init_audio"+ str(i))
            # exit()
            noisy_audio = noise_scale_sqrt * (batch_label-init_audio) + (1.0 - noise_scale) ** 0.5 * noise  # pirorgrad
            predicted = self.model_ddpm(noisy_audio, init_audio, t)  # epsilon^hat
        elif self.deltamu:
            noisy_audio = noise_scale_sqrt * batch_label + (1.0 - noise_scale) ** 0.5 * (noise + init_audio)
            predicted = self.model_ddpm(noisy_audio, t)  # epsilon^hat
        else:
            noisy_audio = noise_scale_sqrt * (batch_label) + (1.0 - noise_scale) ** 0.5 * noise
            predicted = self.model_ddpm(noisy_audio, batch_feat, t)  # epsilon^hat

        if self.args.sigma:
            loss_ddpm = com_mse_sigma_loss(predicted, noise, batch_frame_num_list, mask)
        else:
            loss_ddpm = eval(self.config.train.loss)(predicted, noise , batch_frame_num_list)

        loss = self.config.train.lam * loss_ddpm + loss_dis


        wandb.log(
            {
             'dis_loss': loss_dis.item(),
             'ddpm_loss': loss_ddpm.item(),
             'loss_sum': loss.item()
            }
        )
        # loss = loss1
        # wandb.log(
        #     {'loss_sum': loss.item()}
        # )
        loss.backward()
        if self.args.joint:
            self.optimizer.step()
        self.optimizer_ddpm.step()


        return loss

    def train(self):
        prev_cv_loss = float("inf")
        best_cv_loss = float("inf")
        cv_no_impv = 0
        harving = False
        for epoch in range(self.config.train.n_epochs):
            logging.info(f'Epoch {epoch}')
            self.model.train()
            '''train'''
            for batch in tqdm(self.tr_dataloader):
                self.optimizer.zero_grad()
                batch_feat = batch.feats.cuda()
                batch_label = batch.labels.cuda()
                noisy_phase = torch.atan2(batch_feat[:, -1, :, :], batch_feat[:, 0, :, :])  # [B, 1, T, F] noisy_phase means <相成分>, batch_feat means <batch feature> ?
                clean_phase = torch.atan2(batch_label[:, -1, :, :], batch_label[:, 0, :, :])    # torch.atan2 means 双变量反正切函数,值域为（-pi, pi）

                '''four approaches for feature compression'''
                if self.config.train.feat_type == 'normal':
                    batch_feat, batch_label = torch.norm(batch_feat, dim=1), torch.norm(batch_label, dim=1) # [B, 1, T, F] <相应频率下的分量幅度>
                elif self.config.train.feat_type == 'sqrt':
                    batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.5, ( # 范数的平方根？
                        torch.norm(batch_label, dim=1)) ** 0.5
                elif self.config.train.feat_type == 'cubic':
                    batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.3, (
                        torch.norm(batch_label, dim=1)) ** 0.3
                elif self.config.train.feat_type == 'log_1x':
                    batch_feat, batch_label = torch.log(torch.norm(batch_feat, dim=1) + 1), \
                                              torch.log(torch.norm(batch_label, dim=1) + 1)
                if self.config.train.feat_type in ['normal', 'sqrt', 'cubic', 'log_1x']:
                    batch_feat = torch.stack((batch_feat * torch.cos(noisy_phase), batch_feat * torch.sin(noisy_phase)),    # [B, 2, T, F] <相应频率下的分量幅度在 实轴和虚轴的投影>
                                             dim=1)
                    batch_label = torch.stack(
                        (batch_label * torch.cos(clean_phase), batch_label * torch.sin(clean_phase)),
                        dim=1)
                batch_frame_num_list = batch.frame_num_list
                est_list = self.model(batch_feat)   # x_hat = model(y) [B, 2, T, F]

                batch_loss = eval(self.config.train.loss)(est_list, batch_label, batch_frame_num_list)  # loss class: mse...
                batch_loss.backward()
                self.optimizer.step()
                wandb.log(
                    {'train_batch_mse_loss': batch_loss.item()}
                )

            '''evaluate'''
            self.model.eval()
            all_loss_list = []
            all_csig_list, all_cbak_list, all_covl_list, all_pesq_list, all_ssnr_list, all_stoi_list = [], [], [], [], [], []
            with torch.no_grad():
                for batch in tqdm(self.cv_dataloader):
                    batch_feat = batch.feats.cuda()
                    batch_label = batch.labels.cuda()
                    noisy_phase = torch.atan2(batch_feat[:, -1, :, :], batch_feat[:, 0, :, :])  # [B, 1, T, F]
                    clean_phase = torch.atan2(batch_label[:, -1, :, :], batch_label[:, 0, :, :])

                    '''four approaches for feature compression'''
                    if self.config.train.feat_type == 'normal':
                        batch_feat, batch_label = torch.norm(batch_feat, dim=1), torch.norm(batch_label, dim=1)
                    elif self.config.train.feat_type == 'sqrt':
                        batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.5, (
                            torch.norm(batch_label, dim=1)) ** 0.5
                    elif self.config.train.feat_type == 'cubic':
                        batch_feat, batch_label = (torch.norm(batch_feat, dim=1)) ** 0.3, (
                            torch.norm(batch_label, dim=1)) ** 0.3
                    elif self.config.train.feat_type == 'log_1x':
                        batch_feat, batch_label = torch.log(torch.norm(batch_feat, dim=1) + 1), \
                                                  torch.log(torch.norm(batch_label, dim=1) + 1)
                    if self.config.train.feat_type in ['normal', 'sqrt', 'cubic', 'log_1x']:
                        batch_feat = torch.stack(
                            (batch_feat * torch.cos(noisy_phase), batch_feat * torch.sin(noisy_phase)), # [B, 2, T, F]
                            dim=1)
                        batch_label = torch.stack(
                            (batch_label * torch.cos(clean_phase), batch_label * torch.sin(clean_phase)),
                            dim=1)

                    est_list = self.model(batch_feat)   # [B, 2, T, F]
                    # est_list = batch_feat

                    batch_loss = eval(self.config.train.loss)(est_list, batch_label, batch.frame_num_list)
                    # print("evaluate loss: ", batch_loss)
                    batch_result = compare_complex(est_list, batch_label, batch.frame_num_list,
                                                   feat_type=self.config.train.feat_type)   # compute evaluate metrics
                    all_loss_list.append(batch_loss.item())
                    all_csig_list.append(batch_result[0])
                    all_cbak_list.append(batch_result[1])
                    all_covl_list.append(batch_result[2])
                    all_pesq_list.append(batch_result[3])
                    all_ssnr_list.append(batch_result[4])
                    all_stoi_list.append(batch_result[5])

                wandb.log(
                    {
                        'test_mean_mse_loss': np.mean(all_loss_list),   # mean loss in val dataset
                        'test_mean_csig': np.mean(all_csig_list),
                        'test_mean_cbak': np.mean(all_cbak_list),
                        'test_mean_covl': np.mean(all_covl_list),
                        'test_mean_pesq': np.mean(all_pesq_list),
                        'test_mean_ssnr': np.mean(all_ssnr_list),
                        'test_mean_stoi': np.mean(all_stoi_list),
                    }
                )

                cur_avg_loss = np.mean(all_loss_list)

                '''Adjust the learning rate and early stop'''
                if self.config.optim.half_lr > 1:
                    if cur_avg_loss >= prev_cv_loss:
                        cv_no_impv += 1
                        if cv_no_impv == self.config.optim.half_lr: # adjust lr depend on cv_no_impv
                            harving = True
                        if cv_no_impv >= self.config.optim.early_stop > 0:  # early stop
                            logging.info("No improvement and apply early stop")
                            break
                    else:
                        cv_no_impv = 0

                if harving == True:
                    optim_state = self.optimizer.state_dict()
                    for i in range(len(optim_state['param_groups'])):
                        optim_state['param_groups'][i]['lr'] = optim_state['param_groups'][i]['lr'] / 2.0
                    self.optimizer.load_state_dict(optim_state)
                    logging.info('Learning rate adjusted to %5f' % (optim_state['param_groups'][0]['lr']))
                    harving = False
                prev_cv_loss = cur_avg_loss

                if cur_avg_loss < best_cv_loss:
                    logging.info(
                        f"last best loss is: {best_cv_loss}, current loss is: {cur_avg_loss}, save best_checkpoint.pth")
                    best_cv_loss = cur_avg_loss
                    states = [
                        self.model.state_dict(),
                        self.optimizer.state_dict(),
                    ]
                    torch.save(states, os.path.join(self.args.checkpoint, 'best_checkpoint.pth'))
            # save latest checkpoint
            states = [
                self.model.state_dict(),
                self.optimizer.state_dict(),
            ]
            torch.save(states, os.path.join(self.args.checkpoint, f'checkpoint_{epoch}.pth'))

    def generate_wav(self, load_pre_train=True, data_path='data/noisy_testset_wav'):
        if load_pre_train:
            # load pretrained_model
            pretrained_data = torch.load(os.path.join(self.args.checkpoint, 'best_checkpoint.pth'))
            if self.args.retrain:
                pretrained_data = torch.load(os.path.join(self.args.checkpoint, 'best_checkpoint.pth'))
                self.model.load_state_dict(pretrained_data[0])
                self.optimizer.load_state_dict(pretrained_data[1])
                if self.args.draw or self.args.joint:
                    self.model_ddpm.load_state_dict(pretrained_data[2])
                    self.optimizer_ddpm.load_state_dict(pretrained_data[3])
        self.model.eval()
        torch.backends.cudnn.enabled = True
        alpha, beta, alpha_cum, sigmas, T = self.inference_schedule(fast_sampling=self.params.fast_sampling)
        data_paths = glob.glob(data_path + '/*.wav')
        '''generate wav'''
        with torch.no_grad():
            for path in tqdm(data_paths):
                feat_wav, _ = librosa.load(path, sr=16000)
                c = np.sqrt(np.sum((feat_wav ** 2)) / len(feat_wav))
                feat_wav = feat_wav / c
                feat_wav = torch.FloatTensor(feat_wav)
                '''这里没有像 train 的时候 进行补零(collate.collate_fn) 虽然不会对输入model的数据维数产生影响，会不会对 wav_len < chunk_length 的样本产生影响'''
                feat_x = torch.stft(feat_wav,
                                    n_fft=self.config.train.fft_num,
                                    hop_length=self.config.train.win_shift,
                                    win_length=self.config.train.win_size,
                                    window=torch.hann_window(self.config.train.fft_num)).permute(2, 1, 0).cuda()
                feat_phase_x = torch.atan2(feat_x[-1, :, :], feat_x[0, :, :])
                if self.config.train.feat_type == 'sqrt':
                    feat_mag_x = torch.norm(feat_x, dim=0)
                    feat_mag_x = feat_mag_x ** 0.5
                feat_x = torch.stack(
                    (feat_mag_x * torch.cos(feat_phase_x), feat_mag_x * torch.sin(feat_phase_x)),
                    dim=0)  # [2, T, F]
                # 补充 model ddpm
                batch_feat = feat_x.unsqueeze(dim=0)

                init_audio = self.model(batch_feat)  # [B, 2, T, F]
                init_audio /= self.c
                batch_feat /= self.c
                # print( batch_label[0][0]/self.c)
                # print( init_audio[0][0])    # 矩阵最后几行值相同且与label不同
                # exit()
                if self.deltamu:
                    audio = torch.randn_like(init_audio) + init_audio
                else:
                    audio = torch.randn_like(init_audio)  # XT = N(0, I),  [N, 2, T, F]
                if self.args.sigma:
                    tmp = torch.flatten(torch.abs(init_audio), start_dim=2)
                    tmp /= torch.max(tmp, dim=2, keepdim=True).values
                    tmp = tmp / 2 + 0.5
                    mask = tmp.view(batch_feat.shape)
                    audio = audio * (mask ** 0.5)
                N = audio.shape[0]
                gamma = [0 for i in alpha]  # the first 2 num didn't use
                for n in range(len(alpha)):
                    gamma[n] = sigmas[n]  # beta^tilde
                # print(gamma)    #[0.715, 0.0095, 0.031, 0.095, 0.220, 0.412]
                gamma[0] = 0.2  # beta^tilde_0 = 0.2
                # print("gamma",gamma)
                for n in range(len(alpha) - 1, -1, -1):
                    c1 = 1 / alpha[n] ** 0.5  # c1 in mu equation
                    c2 = beta[n] / (1 - alpha_cum[n]) ** 0.5  # c2 in mu equation
                    if self.pirorgrad:
                        predicted_noise = self.model_ddpm(audio, init_audio,
                                                          torch.tensor([T[n]], device=audio.device).repeat(N))
                    elif self.deltamu:
                        predicted_noise = self.model_ddpm(audio, torch.tensor([T[n]], device=audio.device).repeat(N))
                    else:
                        predicted_noise = self.model_ddpm(audio, batch_feat,  # z_theta(x_t, condition, t)
                                                          torch.tensor([T[n]], device=audio.device).repeat(N))
                    # print("predicted_noise", predicted_noise.shape)
                    # audio = c1 * ((1-gamma[n])*mu+gamma[n]* (noisy_audio-init_audio))  # 插值
                    audio = c1 * (audio - c2 * predicted_noise)  # mu_theta(x_t, z_theta)
                    # print("________________________________________________________________")
                    # print(predicted_noise)
                    # print("________________________________________________________________")
                    # print("predicted_noise", predicted_noise.shape)
                    # print("c1", c1)
                    # print("c2", c2)
                    # print("predicted_noise", predicted_noise)
                    # print(audio)
                    if n > 0:
                        noise = torch.randn_like(audio)
                        sigma = gamma[n]  # sigma = gamma[n]
                        newsigma = max(0, sigma - c1 * gamma[n])  # ???
                        if self.args.sigma:
                            noise = noise * (mask ** 0.5)
                        audio += newsigma * noise  # x_t-1 = mu_theta + beta^tilde * epsilon

                    # audio = torch.clamp(audio, -35/11, 35/11) # Diffuse/ILVR used after preprocess
                if self.pirorgrad:
                    audio += init_audio
                audio *= self.c
                init_audio *= self.c
                esti_x = audio.squeeze(dim=0)

                # print("esti_x", esti_x.shape)
                # exit()
                # istft
                esti_mag, esti_phase = torch.norm(esti_x, dim=0), torch.atan2(esti_x[-1, :, :],
                                                                              esti_x[0, :, :])
                if self.config.train.feat_type == 'sqrt':
                    esti_mag = esti_mag ** 2
                    esti_com = torch.stack((esti_mag * torch.cos(esti_phase), esti_mag * torch.sin(esti_phase)), dim=0)
                tf_esti = esti_com.permute(2, 1, 0).cpu()
                t_esti = torch.istft(tf_esti,
                                     n_fft=self.config.train.fft_num,
                                     hop_length=self.config.train.win_shift,
                                     win_length=self.config.train.win_size,
                                     window=torch.hann_window(self.config.train.fft_num),
                                     length=len(feat_wav)).numpy()
                t_esti = t_esti * c
                raw_path = path.split('/')[-1]
                sf.write(os.path.join(self.args.generated_wav, raw_path), t_esti, 16000)
        clean_data_path = 'data/clean_testset_wav'
        print("success!")
        exit()
        res = compare(clean_data_path, self.args.generated_wav)
        # res = compare(clean_data_path, data_path)
        pm = np.array([x[0:] for x in res])
        pm = np.mean(pm, axis=0)
        logging.info(f'ref={clean_data_path}')
        logging.info(f'deg={self.args.generated_wav}')
        logging.info('csig:%6.4f cbak:%6.4f covl:%6.4f pesq:%6.4f ssnr:%6.4f stoi:%6.4f' % tuple(pm))
