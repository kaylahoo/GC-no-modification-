import os
import glob
import socket
import logging
import sys

import tensorflow as tf
import neuralgym as ng
from data_from_fnames import DataFromFNames
from mask_from_fnames import DataMaskFromFNames
from inpaint_model import InpaintCAModel
from inpaint_model_gc import InpaintGCModel
from trainer import Trainer

logger = logging.getLogger()

def multigpu_graph_def(model, data_mask_data, guides, config, gpu_id=0, loss_type='g'):

    files = None
    with tf.device('/cpu:0'):
        if config.MASKFROMFILE:
            images, masks = data_mask_data.data_pipeline(config.BATCH_SIZE)
        else:
            images = data_mask_data.data_pipeline(config.BATCH_SIZE)
            masks = None
        # if config.RETURN_FILE:
        #     images, files = data.data_pipeline(config.BATCH_SIZE)
        # else:
        #     images = data.data_pipeline(config.BATCH_SIZE)
        # if mask_data is not None:
        #     masks = mask_data.data_pipeline(config.BATCH_SIZE)
        # else:
        #     masks = None
    if loss_type == 'g':
        _, _, losses = model.build_graph_with_losses(
            images, masks, guides, config, summary=True, reuse=True)
    else:
        _, _, losses = model.build_graph_with_losses(
            images, masks, guides, config, reuse=True)
    if loss_type == 'g':
        return losses['g_loss']
    elif loss_type == 'd':
        return losses['d_loss']
    else:
        raise ValueError('loss type is not supported.')


if __name__ == "__main__":
    config = ng.Config(sys.argv[1])
    if config.GPU_ID != -1:
        ng.set_gpus(config.GPU_ID)
    else:
        ng.get_gpus(config.NUM_GPUS)
    # training data
    # Image Data
    with open(config.DATA_FLIST[config.DATASET][0]) as f:
        fnames = f.read().splitlines()
    # # Mask Data

    if config.MASKFROMFILE:
        with open(config.DATA_FLIST[config.MASKDATASET][0]) as f:
            mask_fnames = f.read().splitlines()
        data_mask_data = DataMaskFromFNames(
        list(zip(fnames, mask_fnames)), [config.IMG_SHAPES, config.MASK_SHAPES], random_crop=config.RANDOM_CROP)
        images, masks = data_mask_data.data_pipeline(config.BATCH_SIZE)
    else:
        data_mask_data = DataFromFNames(
        fnames, config.IMG_SHAPES, random_crop=config.RANDOM_CROP)
        images = data_mask_data.data_pipeline(config.BATCH_SIZE)
        masks = None

    guides = None
    # main model
    model = InpaintGCModel()
    g_vars, d_vars, losses = model.build_graph_with_losses(
        images, masks, guides, config=config)
    # validation images
    if config.VAL:
        with open(config.DATA_FLIST[config.DATASET][1]) as f:
            val_fnames = f.read().splitlines()
        with open(config.DATA_FLIST[config.MASKDATASET][1]) as f:
            val_mask_fnames = f.read().splitlines()
        # progress monitor by visualizing static images
        for i in range(config.STATIC_VIEW_SIZE):
            static_fnames = val_fnames[i:i+1]

            if config.MASKFROMFILE:
                static_mask_fnames = val_mask_fnames[i:i+1]
                static_images, static_masks = DataMaskFromFNames(
                list(zip(static_fnames,static_mask_fnames)), [config.IMG_SHAPES, config.MASK_SHAPES],
                 nthreads=1, random_crop=config.RANDOM_CROP).data_pipeline(1)
            else:
                static_images = DataFromFNames(
                    static_fnames, config.IMG_SHAPES, nthreads=1,
                    random_crop=config.RANDOM_CROP).data_pipeline(1)
                static_masks = None

            static_inpainted_images = model.build_static_infer_graph(
                static_images, static_masks, static_masks,  config, name='static_view/%d' % i)
    # training settings
    lr = tf.get_variable(
        'lr', shape=[], trainable=False,
        initializer=tf.constant_initializer(1e-4))
    d_optimizer = tf.train.AdamOptimizer(lr, beta1=0.5, beta2=0.9)
    g_optimizer = d_optimizer
    # gradient processor
    if config.GRADIENT_CLIP:
        gradient_processor = lambda grad_var: (
            tf.clip_by_average_norm(grad_var[0], config.GRADIENT_CLIP_VALUE),
            grad_var[1])
    else:
        gradient_processor = None
    # log dir
    log_prefix = 'model_logs/' + '_'.join([
        ng.date_uid(), socket.gethostname(), config.DATASET,
        'MASKED' if config.GAN_WITH_MASK else 'NORMAL',
        config.GAN,config.LOG_DIR])
    # train discriminator with secondary trainer, should initialize before
    # primary trainer.
    discriminator_training_callback = ng.callbacks.SecondaryTrainer(
        pstep=1,
        optimizer=d_optimizer,
        var_list=d_vars,
        max_iters=5,
        graph_def=multigpu_graph_def,
        graph_def_kwargs={
            'model': model, 'data_mask_data': data_mask_data,  "guides":None, 'config': config, 'loss_type': 'd'},
    )
    # train generator with primary trainer
    trainer = Trainer(
        optimizer=g_optimizer,
        var_list=g_vars,
        max_iters=config.MAX_ITERS,
        graph_def=multigpu_graph_def,
        grads_summary=config.GRADS_SUMMARY,
        gradient_processor=gradient_processor,
        graph_def_kwargs={
            'model': model, 'data_mask_data': data_mask_data, "guides":None, 'config': config, 'loss_type': 'g'},
        spe=config.TRAIN_SPE,
        log_dir=log_prefix,
    )
    # add all callbacks
    if not config.PRETRAIN_COARSE_NETWORK:
        trainer.add_callbacks(discriminator_training_callback)
    trainer.add_callbacks([
        ng.callbacks.WeightsViewer(),
        ng.callbacks.ModelRestorer(trainer.context['saver'], dump_prefix='model_logs/'+config.MODEL_RESTORE+'/snap', optimistic=True),
        ng.callbacks.ModelSaver(config.TRAIN_SPE, trainer.context['saver'], log_prefix+'/snap'),
        ng.callbacks.SummaryWriter((config.VAL_PSTEPS//1), trainer.context['summary_writer'], tf.summary.merge_all()),
    ])
    # launch training
    trainer.train()
