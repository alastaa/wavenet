from concurrent.futures import ProcessPoolExecutor
from functools import partial
import numpy as np
import os
import audio
import torch

from nnmnkwii import preprocessing as P
from wavenet_hparams import hparams
from os.path import exists, basename, splitext
import librosa
from glob import glob
from os.path import join

from wavenet_vocoder.util import is_mulaw_quantize, is_mulaw, is_raw

import sys
sys.path.append('../tacotron2/')
from layers import TacotronSTFT


class MelSpectrogramCreator():

    tacotron_stft = TacotronSTFT(
        hparams.fft_size, hparams.hop_size, hparams.win_length,
        hparams.num_mels, hparams.sample_rate, hparams.fmin,
        hparams.fmax)

    @classmethod
    def mel_spectrogram(cls, wav, method):
        if method == 'original':
            mel = audio.logmelspectrogram(wav)
        elif method == 'tacotron':
            wav_tensor = torch.Tensor(wav).unsqueeze(0)
            mel_tensor = cls.tacotron_stft.mel_spectrogram(wav_tensor)
            mel = mel_tensor.squeeze().data.numpy()
        else:
            raise ValueError
        return mel.astype(np.float32).T


def build_from_path(in_dir, out_dir, num_workers=1, tqdm=lambda x: x,
                    mel_method='original'):
    executor = ProcessPoolExecutor(max_workers=num_workers)
    futures = []
    index = 1
    src_files = sorted(glob(join(in_dir, "**/*.wav"), recursive=True))
    for wav_path in src_files:
        futures.append(executor.submit(
            partial(_process_utterance, out_dir,
                    index, wav_path, "dummy", mel_method)))
        index += 1
    return [future.result() for future in tqdm(futures)]


def _get_speaker_from_path(path):
    speaker_str_mapping = {
            '01m':0,
            '02m':1,
            '03m':2,
            '01n':3,
            '02n':4,
            '03n':5,
    }
    speaker_str = path.split('_')[1]
    return speaker_str_mapping[speaker_str]


def _process_utterance(out_dir, index, wav_path, text, mel_method):
    # Load the audio to a numpy array:
    wav = audio.load_wav(wav_path)

    # Trim begin/end silences
    # NOTE: the threshold was chosen for clean signals
    wav, _ = librosa.effects.trim(wav, top_db=60, frame_length=2048, hop_length=512)

    if hparams.highpass_cutoff > 0.0:
        wav = audio.low_cut_filter(wav, hparams.sample_rate, hparams.highpass_cutoff)

    # Mu-law quantize
    if is_mulaw_quantize(hparams.input_type):
        # Trim silences in mul-aw quantized domain
        silence_threshold = 0
        if silence_threshold > 0:
            # [0, quantize_channels)
            out = P.mulaw_quantize(wav, hparams.quantize_channels - 1)
            start, end = audio.start_and_end_indices(out, silence_threshold)
            wav = wav[start:end]
        constant_values = P.mulaw_quantize(0, hparams.quantize_channels - 1)
        out_dtype = np.int16
    elif is_mulaw(hparams.input_type):
        # [-1, 1]
        constant_values = P.mulaw(0.0, hparams.quantize_channels - 1)
        out_dtype = np.float32
    else:
        # [-1, 1]
        constant_values = 0.0
        out_dtype = np.float32

    wav = np.clip(wav, -1.0, 1.0)
    # Compute a mel-scale spectrogram from the trimmed wav:
    # (N, D)
    mel_spectrogram = MelSpectrogramCreator.mel_spectrogram(wav, mel_method)

    if hparams.global_gain_scale > 0:
        wav *= hparams.global_gain_scale

    # Time domain preprocessing
    if hparams.preprocess is not None and hparams.preprocess not in ["", "none"]:
        f = getattr(audio, hparams.preprocess)
        wav = f(wav)

    # Clip
    if np.abs(wav).max() > 1.0:
        print("""Warning: abs max value exceeds 1.0: {}""".format(np.abs(wav).max()))
        # ignore this sample
        return ("dummy", "dummy", -1, "dummy")


    # Set waveform target (out)
    if is_mulaw_quantize(hparams.input_type):
        out = P.mulaw_quantize(wav, hparams.quantize_channels - 1)
    elif is_mulaw(hparams.input_type):
        out = P.mulaw(wav, hparams.quantize_channels - 1)
    else:
        out = wav

    # zero pad
    # this is needed to adjust time resolution between audio and mel-spectrogram
    l, r = audio.pad_lr(out, hparams.fft_size, audio.get_hop_size())
    if l > 0 or r > 0:
        out = np.pad(out, (l, r), mode="constant", constant_values=constant_values)
    N = mel_spectrogram.shape[0]
    assert len(out) >= N * audio.get_hop_size()

    # time resolution adjustment
    # ensure length of raw audio is multiple of hop_size so that we can use
    # transposed convolution to upsample
    out = out[:N * audio.get_hop_size()]
    assert len(out) % audio.get_hop_size() == 0

    # Write the spectrograms to disk:
    name = splitext(basename(wav_path))[0]
    audio_filename = '%s-wave.npy' % (name)
    mel_filename = '%s-feats.npy' % (name)
    np.save(os.path.join(out_dir, audio_filename),
            out.astype(out_dtype), allow_pickle=False)
    np.save(os.path.join(out_dir, mel_filename),
            mel_spectrogram.astype(np.float32), allow_pickle=False)

    # Return a tuple describing this training example:
    speaker_id = _get_speaker_from_path(audio_filename)
    return (audio_filename, mel_filename, N, text, speaker_id)
