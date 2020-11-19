import argparse
import numpy as np
import os
from pathlib import Path
import json
import pickle
import tensorflow as tf
from tensorflow.keras.callbacks import *
from tensorflow.keras.losses import *
from tensorflow.keras.metrics import *
from tensorflow.keras.optimizers import *
from tensorflow.keras.layers import Conv2D, ZeroPadding2D, Concatenate, BatchNormalization, Dropout
import tensorflow.keras.backend as K
from tensorflow.keras.regularizers import Regularizer
from tensorflow.keras.utils import multi_gpu_model
import tensorflow_addons as tfa

import efficientnet.model as model
from metrics import *
from pipeline import *
from swa import SWA
from train_frame import custom_scheduler
from transforms import *
from utils import *
from models import transformer_layer
from adamp_tf import AdamP

np.set_printoptions(precision=4)


args = argparse.ArgumentParser()
args.add_argument('--name', type=str, required=True)
args.add_argument('--model', type=str, default='EfficientNetB0')
args.add_argument('--gpus', type=int, default=[0], nargs='+')
args.add_argument('--mode', type=str, default='GRU',
                                 choices=['GRU', 'transformer'])
args.add_argument('--pretrain', type=bool, default=False)
args.add_argument('--transfer', default=False, action='store_true')
args.add_argument('--n_layers', type=int, default=0)
args.add_argument('--n_dim', type=int, default=256)
args.add_argument('--n_heads', type=int, default=8)
args.add_argument('--n_classes', type=int, default=30)
args.add_argument('--n_mels', type=int, default=128)

# DATA
args.add_argument('--background_sounds', type=str,
                  default='generate_wavs/codes/drone_normed_complex_v2.pickle')
args.add_argument('--voices', type=str,
                  default='generate_wavs/codes/voice_normed_complex.pickle')
args.add_argument('--labels', type=str,
                  default='generate_wavs/codes/voice_labels_mfc.npy')
args.add_argument('--noises', type=str,
                  default='generate_wavs/codes/noises_specs.pickle')
args.add_argument('--test', default=True, action='store_false')
args.add_argument('--test_background_sounds', type=str,
                  default='generate_wavs/codes/test_drone_normed_complex.pickle')
args.add_argument('--test_voices', type=str,
                  default='generate_wavs/codes/test_voice_normed_complex.pickle')
args.add_argument('--test_labels', type=str,
                  default='generate_wavs/codes/test_voice_labels_mfc.npy')

# args.add_argument('--background_sounds', type=str,
#                   default='generate_wavs/codes/drone_normed_complex_v3.pickle')
# args.add_argument('--voices', type=str,
#                   default='generate_wavs/codes/voice_normed_complex_v2_2.pickle')
# args.add_argument('--labels', type=str,
#                   default='generate_wavs/codes/voice_labels_mfc_v2_1.npy')
# args.add_argument('--noises', type=str,
#                   default='generate_wavs/codes/noises_specs.pickle')
# args.add_argument('--test', default=True, action='store_false')
# args.add_argument('--test_background_sounds', type=str,
#                   default='generate_wavs/codes/test_drone_normed_complex_v2.pickle')
# args.add_argument('--test_voices', type=str,
#                   default='generate_wavs/codes/test_voice_normed_complex.pickle')
# args.add_argument('--test_labels', type=str,
#                   default='generate_wavs/codes/test_voice_labels_mfc.npy')

# TRAINING
args.add_argument('--optimizer', type=str, default='adam',
                                 choices=['adam', 'sgd', 'rmsprop', 'adamp', 'adamw'])
args.add_argument('--lr', type=float, default=0.001)
args.add_argument('--lr_factor', type=float, default=0.7)
args.add_argument('--lr_patience', type=int, default=10)
args.add_argument('--lr_scheduler', type=str, default='cos',
                                    choices=['rsqrt', 'cos'])

args.add_argument('--epochs', type=int, default=500)
args.add_argument('--batch_size', type=int, default=12)
args.add_argument('--n_frame', type=int, default=2048)
args.add_argument('--steps_per_epoch', type=int, default=100)
args.add_argument('--val_steps_per_epoch', type=int, default=24)
args.add_argument('--l2', type=float, default=1e-4)

# AUGMENTATION
args.add_argument('--alpha', type=float, default=0.8)
args.add_argument('--snr', type=float, default=-15)
args.add_argument('--max_voices', type=int, default=10)
args.add_argument('--max_noises', type=int, default=5)

args.add_argument('--reverse', type=bool, default=True)
args.add_argument('--multiplier', type=float, default=10.)


def safe_div(x, y, eps=EPSILON):
    # returns safe x / max(y, epsilon)
    return x / tf.maximum(y, eps)


def minmax_log_on_mel(mel, labels=None):
    axis = tuple(range(1, len(mel.shape)))

    # MIN-MAX
    mel_max = tf.math.reduce_max(mel, axis=axis, keepdims=True)
    mel_min = tf.math.reduce_min(mel, axis=axis, keepdims=True)
    mel = safe_div(mel-mel_min, mel_max-mel_min)
    
    # LOG
    #mel = tf.math.log((mel + 1.) * 10.) #2.71828 -18.420680743952367
    mel = tf.math.log(mel + EPSILON)
    # mel1 = mel * -1.
    # mel2 = mel + 18.420680743952367
    # mel3 = tf.abs(tf.abs(mel + mel2) - 18.420680743952367)
    # mel = tf.concat([mel1, mel2, mel3], axis=-1)

    if labels is not None:
        return mel, labels
    return mel


# TODO: need to fix this function
def random_reverse_chan(specs, labels):
    # Assume MelSpectrogram
    # specs: [..., freq, time, chan]
    # labels: [..., time', 30]
    rev_specs = tf.reverse(specs, [-1])
    rev_labels = tf.stack(
        tf.split(labels, 3, axis=-1), axis=-2) # [..., time', 3, 10]
    rev_labels = tf.reverse(rev_labels, [-1])
    rev_labels = tf.concat(
        tf.unstack(rev_labels, axis=-2), axis=-1)

    coin = tf.cast(tf.random.uniform([]) > 0.5, tf.float32)

    specs = coin * specs + (1-coin) * rev_specs
    labels = coin * labels + (1-coin) * rev_labels
                          
    raise Exception()
    return specs, labels


def augment(specs, labels, time_axis=1, freq_axis=0):
    specs = mask(specs, axis=time_axis, max_mask_size=16, n_mask=6) # time
    specs = mask(specs, axis=freq_axis, max_mask_size=8) # freq
    # specs, labels = random_reverse_chan(specs, labels)
    return specs, labels


def make_preprocess_labels(multiplier):
    def preprocess_labels(x, y):
        # process y: [None, time, classes] -> [None, time', classes]
        for i in range(5):
            # sum_pool1d
            y = tf.nn.avg_pool1d(y, 2, strides=2, padding='SAME') * 2
        y *= multiplier
        return x, y
    return preprocess_labels


def to_density_labels(x, y):
    """
    :param y: [..., n_voices, n_frames, n_classes]
    :return: [..., n_frames, n_classes]
    """
    y = safe_div(y, tf.reduce_sum(y, axis=(-2, -1), keepdims=True))
    y = tf.reduce_sum(y, axis=-3)
    return x, y


def make_dataset(config, training=True):
    # Load required datasets
    if training:
        backgrounds = load_data(config.background_sounds)
        voices = load_data(config.voices)
        labels = load_data(config.labels)
    else:
        backgrounds = load_data(config.test_background_sounds)
        voices = load_data(config.test_voices)
        labels = load_data(config.test_labels)
    labels = np.eye(30, dtype='float32')[labels] # to one-hot vectors
    noises = load_data(config.noises)

    # Make pipeline and process the pipeline
    pipeline = make_pipeline(backgrounds, 
                             voices, labels,
                             noises,
                             n_frame=config.n_frame,
                             max_voices=config.max_voices,
                             max_noises=config.max_noises,
                             n_classes=30,
                             snr=config.snr)
    pipeline = pipeline.map(to_density_labels)
    if training:
        pipeline = pipeline.map(augment)
    pipeline = pipeline.batch(config.batch_size, drop_remainder=False)
    pipeline = pipeline.map(complex_to_magphase)
    pipeline = pipeline.map(magphase_to_mel(config.n_mels))
    pipeline = pipeline.map(minmax_log_on_mel)
    pipeline = pipeline.map(make_preprocess_labels(config.multiplier))
    return pipeline.prefetch(AUTOTUNE)


def make_d_total(multiplier):
    def d_total(y_true, y_pred, apply_round=True):
        y_true /= multiplier
        y_pred /= multiplier

        # [None, time, 30] -> [None, time, 3, 10]
        y_true = tf.stack(tf.split(y_true, 3, axis=-1), axis=-2)
        y_pred = tf.stack(tf.split(y_pred, 3, axis=-1), axis=-2)

        # d_dir
        d_true = tf.reduce_sum(y_true, axis=(-3, -2))
        d_pred = tf.reduce_sum(y_pred, axis=(-3, -2))
        if apply_round:
            d_true = tf.math.round(d_true)
            d_pred = tf.math.round(d_pred)
        d_dir = D_direction(d_true, d_pred)

        # c_cls
        c_true = tf.reduce_sum(y_true, axis=(-3, -1))
        c_pred = tf.reduce_sum(y_pred, axis=(-3, -1))
        if apply_round:
            c_true = tf.math.round(c_true)
            c_pred = tf.math.round(c_pred)
        d_cls = D_class(c_true, c_pred)

        return 0.8 * d_dir + 0.2 * d_cls
    return d_total


def custom_loss(y_true, y_pred, alpha=0.8, l2=1.0):
    # y_true, y_pred = [None, time, 30]
    # [None, time, 30] -> [None, time, 3, 10]
    t_true = tf.stack(tf.split(y_true, 3, axis=-1), axis=-2)
    t_pred = tf.stack(tf.split(y_pred, 3, axis=-1), axis=-2)

    # [None, time, 10]
    d_y_true = tf.reduce_sum(t_true, axis=-2)
    d_y_pred = tf.reduce_sum(t_pred, axis=-2)

    # [None, time, 3]
    c_y_true = tf.reduce_sum(t_true, axis=-1)
    c_y_pred = tf.reduce_sum(t_pred, axis=-1)

    loss = alpha * tf.keras.losses.MAE(tf.reduce_sum(d_y_true, axis=1),
                                       tf.reduce_sum(d_y_pred, axis=1)) \
         + (1-alpha) * tf.keras.losses.MAE(tf.reduce_sum(c_y_true, axis=1),
                                           tf.reduce_sum(c_y_pred, axis=1))

    # TODO: OT loss

    # TV: total variation loss
    # normed - degrees [None, time, 10]
    n_d_true = safe_div(
        d_y_true, tf.reduce_sum(d_y_true, axis=1, keepdims=True))
    n_d_pred = safe_div(
        d_y_pred, tf.reduce_sum(d_y_pred, axis=1, keepdims=True))

    # normed - classes [None, time, 3]
    n_c_true = safe_div(
        c_y_true, tf.reduce_sum(c_y_true, axis=1, keepdims=True))
    n_c_pred = safe_div(
        c_y_pred, tf.reduce_sum(c_y_pred, axis=1, keepdims=True))

    tv = alpha * tf.reduce_mean(
            tf.reduce_sum(tf.math.abs(n_d_true - n_d_pred), axis=1) 
            * tf.reduce_sum(d_y_true, axis=1), # [None, 10]
            axis=1)
    tv += (1-alpha) * tf.reduce_mean(
            tf.reduce_sum(tf.math.abs(n_c_true - n_c_pred), axis=1) 
            * tf.reduce_sum(c_y_true, axis=1), # [None, 3]
            axis=1)
    loss += l2 * tv

    return loss


def cos_sim(y_true, y_pred):
    mask = tf.cast(
        tf.reduce_sum(y_true, axis=-2) > 0., tf.float32) # [None, 30]
    mask = safe_div(mask, tf.reduce_sum(mask, axis=-1, keepdims=True))
    return tf.reduce_sum(
        tf.keras.losses.cosine_similarity(y_true, y_pred, axis=-2) * mask, 
        axis=-1)


def warmup_cosine_decay(lr=0.1, epochs=500, warmup=10):
    def func(epoch):
        if epoch <= warmup:
            return lr * ((epoch + 1) / (warmup + 1)) ** 2
        else:
            return lr * 0.5 * (1 + np.cos(np.pi * (epoch - warmup) / np.float32(epochs - warmup)))
    return func


class Spec_Filter(tf.keras.layers.Layer):
    """
    Filter on mag
    input shape must be as [batch, freq, time, channels]
    """
    def __init__(self, N_filt=128, initialize_mel=True, use_bias=False,
                fs=16000, nfft=257):
        super().__init__()
        self.N_filt = N_filt
        self.use_bias = use_bias
        self.initialize_mel = initialize_mel
        self.fs = fs
        self.nfft = nfft
        
    def build(self, input_shape):
        if self.initialize_mel:
            w_init = tf.signal.linear_to_mel_weight_matrix(
                    self.N_filt, self.nfft, self.fs)
        else:
            w_init = tf.random_normal_initializer()(shape=(input_shape[-1], self.N_filt),
                                                    dtype='float32')              
        self.w = tf.Variable(
            initial_value=w_init,
            trainable=True,
            name='Filt_w'
        )
        
        b_init = tf.zeros_initializer()
        self.b = tf.Variable(
            b_init(shape=(self.N_filt,), dtype='float32'),
            trainable=self.use_bias,
            name='Filt_b'
            )
    
    def call(self, inputs):
        x = tf.tensordot(inputs, self.w, axes=(-3, 0)) + self.b
        x = tf.transpose(x, perm=[0, 3, 1, 2])
        return x
    
    def get_config(self):
        base_config = super().get_config().copy()
        base_config.update({
            "N_filt": self.N_filt,
            "use_bias" : self.use_bias,
            "initialize_mel":self.initialize_mel,
            "fs":self.fs,
            "nfft":self.nfft
            })
        return dict(list(base_config.items()))


if __name__ == "__main__":
    config = args.parse_args()
    print(config)

    TOTAL_EPOCH = config.epochs
    BATCH_SIZE = config.batch_size


    """ LOG """
    NAME = Path('logs', config.name)
    CSV_NAME = NAME / (config.name + '.csv')
    H5_NAME = NAME / (config.name + '.h5')
    SWA_NAME = NAME / (config.name + '_SWA.h5')
    os.system(f'mkdir -p {str(NAME)}')
    os.system(f'touch {str(CSV_NAME)}')
    json.dump(config.__dict__, open(str(NAME / (config.name + '.json')), 'w'), indent=2, sort_keys=False)


    """ MODEL """
    x = tf.keras.layers.Input(shape=(config.n_mels, config.n_frame, 2))
    #x = BatchNormalization(axis=-1, momentum=0.99)(x)
    model = getattr(model, config.model)(
        include_top=False,
        weights=None,
        input_tensor=x,
        backend=tf.keras.backend,
        layers=tf.keras.layers,
        models=tf.keras.models,
        utils=tf.keras.utils,
    )
    if config.transfer:
        load_imagenet(model, scale=0, train_type='noisy-student') #'noisy-student', 'imagenet'
    out = model.output
    #out = Dropout(0.2)(out)
    out = tf.transpose(out, perm=[0, 2, 1, 3])
    out = tf.keras.layers.Reshape([-1, out.shape[-1]*out.shape[-2]])(out)

    if config.n_layers > 0:
        if config.mode == 'GRU':
            out = tf.keras.layers.Dense(config.n_dim)(out)
            for i in range(config.n_layers):
                # out = transformer_layer(config.n_dim, config.n_heads)(out)
                out = tf.keras.layers.Bidirectional(
                    tf.keras.layers.GRU(config.n_dim, return_sequences=True),
                    backward_layer=tf.keras.layers.GRU(config.n_dim, 
                                                       return_sequences=True,
                                                       go_backwards=True))(out)
        elif config.mode == 'transformer':
            out = tf.keras.layers.Dense(config.n_dim)(out)
            out = encoder(config.n_layers,
                          config.n_dim,
                          config.n_heads)(out)

            out = tf.keras.layers.Flatten()(out)
            out = tf.keras.layers.ReLU()(out)

    out = tf.keras.layers.Dense(config.n_classes, activation='relu')(out)
    model = tf.keras.models.Model(inputs=model.input, outputs=out)

    if config.pretrain:
        model.load_weights(NAME)
        print('loaded pretrained model')

    if config.optimizer == 'adam':
        opt = Adam(config.lr, clipvalue=0.01)
    elif config.optimizer == 'adamp':
        opt = AdamP(config.lr, clipvalue=0.01)
    elif config.optimizer == 'adamw':
        opt = tfa.optimizers.AdamW(weight_decay=config.l2, learning_rate=config.lr)
    elif config.optimizer == 'sgd':
        opt = SGD(config.lr, momentum=0.9)
    else:
        opt = RMSprop(config.lr, momentum=0.9)

    if config.l2 > 0 and config.optimizer != 'adamw':
        model = apply_kernel_regularizer(
            model, tf.keras.regularizers.l2(config.l2)) #Ortho(config.l2))

    if len(config.gpus) > 1:
        model = multi_gpu_model(model, gpus=len(config.gpus))
    model.compile(optimizer=opt, 
                  loss=custom_loss,
                  metrics=[make_d_total(config.multiplier), cos_sim])
    #model.summary()
    

    """ DATA """
    train_set = make_dataset(config, training=True)
    test_set = make_dataset(config, training=False) if config.test else None


    """ TRAINING """
    if config.lr_scheduler == 'rsqrt':
        lr_scheduler = custom_scheduler(4096, TOTAL_EPOCH/12)
    elif config.lr_scheduler == 'cos':
        lr_scheduler = warmup_cosine_decay(lr=config.lr, epochs=TOTAL_EPOCH, warmup=10)

    callbacks = [
        CSVLogger(str(CSV_NAME),
                  append=True),
        LearningRateScheduler(lr_scheduler),
        SWA(start_epoch=TOTAL_EPOCH//2, swa_freq=2),
        ModelCheckpoint(H5_NAME,#NAME
                        monitor='val_d_total' if config.test else 'd_total', 
                        save_best_only=True,
                        verbose=1),
        TerminateOnNaN()
    ]

    model.fit(train_set,
              epochs=TOTAL_EPOCH,
              batch_size=BATCH_SIZE,
              steps_per_epoch=config.steps_per_epoch,
              validation_data=test_set,
              validation_steps=config.val_steps_per_epoch,
              callbacks=callbacks)

    model.save(str(SWA_NAME))