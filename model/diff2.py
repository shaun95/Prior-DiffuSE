import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from math import sqrt


Linear = nn.Linear
ConvTranspose2d = nn.ConvTranspose2d

class DiffWave(nn.Module):
  def __init__(self, args, params):
    super().__init__()
    self.params = params
    self.input_projection = Conv1d(1, params.residual_channels, 1)
    self.diffusion_embedding = DiffusionEmbedding(len(params.noise_schedule))
    self.spectrogram_upsampler = SpectrogramUpsampler(params.n_mels)
    self.residual_layers = nn.ModuleList([
        # ResidualBlock(params.n_mels, params.residual_channels, 2**(i % params.dilation_cycle_length), fix_in=args.fix_in, split=args.voicebank)
        ResidualBlock(params.n_mels, params.residual_channels, 2 ** (i % params.dilation_cycle_length))
        for i in range(params.residual_layers)
    ])
    self.skip_projection = Conv1d(params.residual_channels, params.residual_channels, 1)
    self.output_projection = Conv1d(params.residual_channels, 1, 1)
    nn.init.zeros_(self.output_projection.weight)

  def forward(self, audio, audio_init, diffusion_step):
    x = audio.unsqueeze(1)
    x = self.input_projection(x)
    x = F.relu(x) # [8,64, 96922]

    diffusion_step = self.diffusion_embedding(diffusion_step)
    # print("x:", x.shape)
    # print("diffusion_step:", diffusion_step.shape)

    # spectrogram = self.spectrogram_upsampler(audio_init)  #  [8, 301, 322] ->
    audio_init = audio_init.unsqueeze(1)
    spectrogram = self.input_projection(audio_init) # [8,64, 96922]
    spectrogram = F.relu(spectrogram)
    # print("spectrogram:", spectrogram.shape)

    skip = []
    for layer in self.residual_layers:
      x, skip_connection = layer(x, spectrogram, diffusion_step)
      skip.append(skip_connection)

    x = torch.sum(torch.stack(skip), dim=0) / sqrt(len(self.residual_layers))
    x = self.skip_projection(x)
    x = F.relu(x)
    x = self.output_projection(x)
    return x

def Conv1d(*args, **kwargs):
  layer = nn.Conv1d(*args, **kwargs)
  nn.init.kaiming_normal_(layer.weight)
  return layer


@torch.jit.script
def silu(x):
  return x * torch.sigmoid(x)


class DiffusionEmbedding(nn.Module):
  def __init__(self, max_steps):
    super().__init__()
    self.register_buffer('embedding', self._build_embedding(max_steps), persistent=False)
    self.projection1 = Linear(128, 512)
    self.projection2 = Linear(512, 512)

  def forward(self, diffusion_step):
    if diffusion_step.dtype in [torch.int32, torch.int64]:
      x = self.embedding[diffusion_step]
    else:
      x = self._lerp_embedding(diffusion_step)
    x = self.projection1(x)
    x = silu(x)
    x = self.projection2(x)
    x = silu(x)
    return x

  def _lerp_embedding(self, t):
    low_idx = torch.floor(t).long()
    high_idx = torch.ceil(t).long()
    low = self.embedding[low_idx]
    high = self.embedding[high_idx]
    return low + (high - low) * (t - low_idx)

  def _build_embedding(self, max_steps):
    steps = torch.arange(max_steps).unsqueeze(1)  # [T,1]
    dims = torch.arange(64).unsqueeze(0)          # [1,64]
    table = steps * 10.0**(dims * 4.0 / 63.0)     # [T,64]
    table = torch.cat([torch.sin(table), torch.cos(table)], dim=1)
    return table


class SpectrogramUpsampler(nn.Module):
  def __init__(self, n_mels):
    super().__init__()
    self.conv1 = ConvTranspose2d(1, 1, [3, 32], stride=[1, 16], padding=[1, 8])
    self.conv2 = ConvTranspose2d(1, 1,  [3, 32], stride=[1, 16], padding=[1, 8])

  def forward(self, x):
    x = torch.unsqueeze(x, 1)
    x = self.conv1(x)
    x = F.leaky_relu(x, 0.4)
    x = self.conv2(x)
    x = F.leaky_relu(x, 0.4)
    x = torch.squeeze(x, 1)
    return x


class ResidualBlock(nn.Module):
  def __init__(self, n_mels, residual_channels, dilation, fix_in=False, split=False):
    super().__init__()
    self.dilated_conv = Conv1d(residual_channels, 2 * residual_channels, 3, padding=dilation, dilation=dilation)
    self.diffusion_projection = Linear(512, residual_channels)
    self.conditioner_projection = Conv1d(residual_channels, 2 * residual_channels, 3, padding=dilation, dilation=dilation)
    self.split = split
    self.fix_in = fix_in
    if self.split:
      # print("2 individual Conv1d")
      self.output_projection = Conv1d(residual_channels, residual_channels, 1)
      self.output_residual = Conv1d(residual_channels, residual_channels, 1)
    else:
      if self.fix_in:
        print("1 big and 1 small Conv1d")
        self.output_projection = Conv1d(residual_channels, 2 * residual_channels, 1)
        self.output_residual = Conv1d(residual_channels, residual_channels, 1)
      else:
        print("1 big Conv1d")
        self.output_projection = Conv1d(residual_channels, 2 * residual_channels, 1)

  def forward(self, x, conditioner, diffusion_step):
    diffusion_step = self.diffusion_projection(diffusion_step).unsqueeze(-1)
    conditioner = self.conditioner_projection(conditioner)

    y = x + diffusion_step
    y = self.dilated_conv(y) + conditioner

    gate, filter = torch.chunk(y, 2, dim=1)
    y = torch.sigmoid(gate) * torch.tanh(filter)

    if self.split:
      residual = self.output_residual(y)
      skip = self.output_projection(y)
    elif self.fix_in:
      #calculate residual from non-fixed parameter
      residual = self.output_residual(y)
      #calculate skip from fixed parameter
      y = self.output_projection(y)
      _ , skip = torch.chunk(y, 2, dim=1)
    else:
      y = self.output_projection(y)
      residual, skip = torch.chunk(y, 2, dim=1)

    return (x + residual) / sqrt(2.0), skip



