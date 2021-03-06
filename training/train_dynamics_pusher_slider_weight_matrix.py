import os
import random
import numpy as np
import time

# from progressbar import ProgressBar

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR
from torch.utils.data import Dataset, DataLoader
from torch.utils.tensorboard import SummaryWriter

from key_dynam.dataset.episode_dataset import MultiEpisodeDataset
from key_dynam.dynamics.utils import rand_int, count_trainable_parameters, Tee, AverageMeter, get_lr, to_np, set_seed
from key_dynam.dataset.function_factory import ObservationFunctionFactory, ActionFunctionFactory
from key_dynam.dataset.utils import load_drake_sim_episodes_from_config
from key_dynam.utils.utils import save_yaml, load_pickle, get_data_root, save_pickle
from key_dynam.dynamics.models_dy import rollout_model, get_object_and_robot_state_indices, DynaNetMLPWeightMatrix
from key_dynam.models.model_builder import build_dynamics_model



"""
Training for DrakePusherSlider without vision
"""

DEBUG = False

def save_model(model, save_base_path):
    # save both the model in binary form, and also the state dict
    torch.save(model.state_dict(), save_base_path + "_state_dict.pth")
    torch.save(model, save_base_path + "_model.pth")

def train_dynamics(config,
                   train_dir,  # str: directory to save output
                   multi_episode_dict=None,
                   visual_observation_function=None,
                   metadata=None,
                   spatial_descriptors_data=None,
                   ):


    assert multi_episode_dict is not None
    # assert spatial_descriptors_idx is not None

    # set random seed for reproduction
    set_seed(config['train']['random_seed'])

    st_epoch = config['train']['resume_epoch'] if config['train']['resume_epoch'] > 0 else 0
    tee = Tee(os.path.join(train_dir, 'train_st_epoch_%d.log' % st_epoch), 'w')

    tensorboard_dir = os.path.join(train_dir, "tensorboard")
    if not os.path.exists(tensorboard_dir):
        os.makedirs(tensorboard_dir)

    writer = SummaryWriter(log_dir=tensorboard_dir)

    # save the config
    save_yaml(config, os.path.join(train_dir, "config.yaml"))

    if metadata is not None:
        save_pickle(metadata, os.path.join(train_dir, 'metadata.p'))

    if spatial_descriptors_data is not None:
        save_pickle(spatial_descriptors_data, os.path.join(train_dir, 'spatial_descriptors.p'))


    training_stats = dict()
    training_stats_file = os.path.join(train_dir, 'training_stats.yaml')

    # load the data

    action_function = ActionFunctionFactory.function_from_config(config)
    observation_function = ObservationFunctionFactory.function_from_config(config)

    datasets = {}
    dataloaders = {}
    data_n_batches = {}
    for phase in ['train', 'valid']:
        print("Loading data for %s" % phase)
        datasets[phase] = MultiEpisodeDataset(config,
                                              action_function=action_function,
                                              observation_function=observation_function,
                                              episodes=multi_episode_dict,
                                              phase=phase,
                                              visual_observation_function=visual_observation_function)

        dataloaders[phase] = DataLoader(
            datasets[phase], batch_size=config['train']['batch_size'],
            shuffle=True if phase == 'train' else False,
            num_workers=config['train']['num_workers'], drop_last=True)

        data_n_batches[phase] = len(dataloaders[phase])

    use_gpu = torch.cuda.is_available()

    # compute normalization parameters if not starting from pre-trained network . . .


    '''
    Build model for dynamics prediction
    '''
    model_dy = build_dynamics_model(config)

    assert isinstance(model_dy, DynaNetMLPWeightMatrix)

    print("type(model_dy)", type(model_dy))


    # criterion
    criterionMSE = nn.MSELoss()
    l1Loss = nn.L1Loss()
    smoothL1 = nn.SmoothL1Loss()

    # optimizer
    params = model_dy.parameters()
    lr = float(config['train']['lr'])
    optimizer = optim.Adam(params, lr=lr, betas=(config['train']['adam_beta1'], 0.999))

    # setup scheduler
    sc = config['train']['lr_scheduler']
    scheduler = None

    if config['train']['lr_scheduler']['enabled']:
        if config['train']['lr_scheduler']['type'] == "ReduceLROnPlateau":
            scheduler = ReduceLROnPlateau(optimizer,
                                          mode='min',
                                          factor=sc['factor'],
                                          patience=sc['patience'],
                                          threshold_mode=sc['threshold_mode'],
                                          cooldown= sc['cooldown'],
                                          verbose=True)
        elif config['train']['lr_scheduler']['type'] == "StepLR":
            step_size = config['train']['lr_scheduler']['step_size']
            gamma = config['train']['lr_scheduler']['gamma']
            scheduler = StepLR(optimizer, step_size=step_size, gamma=gamma)
        else:
            raise ValueError("unknown scheduler type: %s" %(config['train']['lr_scheduler']['type']))

    if use_gpu:
        print("using gpu")
        model_dy = model_dy.cuda()

    # print("model_dy.vision_net._ref_descriptors.device", model_dy.vision_net._ref_descriptors.device)
    # print("model_dy.vision_net #params: %d" %(count_trainable_parameters(model_dy.vision_net)))

    best_valid_loss = np.inf
    valid_loss_type = config['train']['valid_loss_type']
    global_iteration = 0
    counters = {'train': 0, 'valid': 0}
    epoch_counter_external = 0
    loss = 0

    index_map = get_object_and_robot_state_indices(config)
    object_state_indices = torch.LongTensor(index_map['object_indices'])
    robot_state_indices = torch.LongTensor(index_map['robot_indices'])

    object_state_shape = config['dataset']['object_state_shape']


    try:
        for epoch in range(st_epoch, config['train']['n_epoch']):
            phases = ['train', 'valid']
            epoch_counter_external = epoch

            writer.add_scalar("Training Params/epoch", epoch, global_iteration)
            for phase in phases:

                # only validate at a certain frequency
                if (phase == "valid") and ((epoch % config['train']['valid_frequency']) != 0):
                    continue

                model_dy.train(phase == 'train')

                average_meter_container = dict()

                step_duration_meter = AverageMeter()

                # bar = ProgressBar(max_value=data_n_batches[phase])
                loader = dataloaders[phase]

                for i, data in enumerate(loader):

                    loss_container = dict() # store the losses for this step

                    step_start_time = time.time()

                    global_iteration += 1
                    counters[phase] += 1

                    with torch.set_grad_enabled(phase == 'train'):
                        n_his, n_roll = config['train']['n_history'], config['train']['n_rollout']
                        n_samples = n_his + n_roll

                        if DEBUG:
                            print("global iteration: %d" %(global_iteration))
                            print("n_samples", n_samples)

                        # [B, n_samples, obs_dim]
                        states = data['observations_combined']

                        # [B, n_samples, action_dim]
                        actions = data['actions']
                        B = actions.shape[0]


                        if use_gpu:
                            states = states.cuda()
                            actions = actions.cuda()

                        # states, actions = data
                        assert actions.shape[1] == n_samples


                        # weighted states
                        z_dict = model_dy.compute_z_state(states)
                        z = z_dict['z']


                        # [B, n_his, state_dim]
                        # state_init = states[:, :n_his]
                        z_init = z[:, :n_his]

                        # We want to rollout n_roll steps
                        # actions = [B, n_his + n_roll, -1]
                        # so we want action_seq.shape = [B, n_roll, -1]
                        action_start_idx = 0
                        action_end_idx = n_his + n_roll - 1
                        action_seq = actions[:, action_start_idx:action_end_idx]

                        if DEBUG:
                            print("states.shape", states.shape)
                            print("actions.shape", actions.shape)
                            print("action_seq.shape", action_seq.shape)

                        # try using models_dy.rollout_model instead of doing this manually
                        # need to input the z_state
                        rollout_data = rollout_model(state_init=z_init,
                                                     action_seq=action_seq,
                                                     dynamics_net=model_dy,
                                                     compute_debug_data=False)

                        # [B, n_roll, state_dim]
                        # state_rollout_pred = rollout_data['state_pred']
                        z_rollout_pred = rollout_data['state_pred']

                        # [B, n_roll, state_dim]
                        state_rollout_gt = states[:, n_his:]
                        z_rollout_gt = model_dy.compute_z_state(state_rollout_gt)['z']

                        z_pred_err = z_rollout_pred - z_rollout_gt


                        loss_mse = criterionMSE(z_rollout_pred, z_rollout_gt)
                        loss_l1 = l1Loss(z_rollout_pred, z_rollout_gt)
                        loss_l2 = torch.norm(z_pred_err, dim=-1).mean()


                        # compute losses at final step of the rollout
                        mse_final_step = criterionMSE(z_rollout_pred[:, -1], z_rollout_gt[:, -1])
                        l2_final_step = torch.norm(z_pred_err[:, -1], dim=-1).mean()
                        l1_final_step = l1Loss(z_rollout_pred[:, -1], z_rollout_gt[:, -1])

                        loss_container['mse'] = loss_mse
                        loss_container['l1'] = loss_l1
                        loss_container['mse_final_step'] = mse_final_step
                        loss_container['l1_final_step'] = l1_final_step
                        loss_container['l2_final_step'] = l2_final_step
                        loss_container['l2'] = loss_l2


                        # sparsity loss on the weight matrix
                        y = 0.5*torch.ones_like(model_dy.weight_matrix)
                        loss_container['weight_matrix_sparsity'] = -2.0*l1Loss(model_dy.weight_matrix, y) + 1

                        # compute the loss
                        loss = 0
                        for key, val in config['loss_function'].items():
                            if val['enabled']:
                                loss += loss_container[key] * val['weight']

                        for key, val in loss_container.items():
                            if not key in average_meter_container:
                                average_meter_container[key] = AverageMeter()

                            average_meter_container[key].update(val.item(), B)

                    step_duration_meter.update(time.time() - step_start_time)

                    if phase == 'train':
                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()

                    if (i % config['train']['log_per_iter'] == 0) or (global_iteration % config['train']['log_per_iter'] == 0):
                        log = '%s [%d/%d][%d/%d] LR: %.6f' % (
                            phase, epoch, config['train']['n_epoch'], i, data_n_batches[phase],
                            get_lr(optimizer))

                        # log += ', l2: %.6f' % (loss_container['l2'].item())
                        # log += ', l2_final_step: %.6f' %(loss_container['l2_final_step'].item())

                        log += ', step time %.6f' %(step_duration_meter.avg)
                        step_duration_meter.reset()


                        print(log)

                        # log data to tensorboard
                        # only do it once we have reached 100 iterations
                        if global_iteration > 100:
                            writer.add_scalar("Params/learning rate", get_lr(optimizer), global_iteration)
                            writer.add_scalar("Loss_train/%s" %(phase), loss.item(), global_iteration)

                            for loss_type, loss_obj in loss_container.items():
                                plot_name = "Loss/%s/%s" %(loss_type, phase)
                                writer.add_scalar(plot_name, loss_obj.item(), counters[phase])


                            # only plot the weights if we are in the train phase . . . .
                            if phase == "train":
                                weight_matrix = model_dy.weight_matrix
                                M, K = weight_matrix.shape
                                threshold = 1.0/K
                                num_active_keypoints = 0
                                for k in range(K):
                                    val = weight_matrix[:, k].sum().item()
                                    plot_name = "Weights/%d" %(k)
                                    writer.add_scalar(plot_name, val, counters[phase])

                                    if torch.max(weight_matrix[:, k]) >= threshold:
                                        num_active_keypoints += 1

                                writer.add_scalar("Num Active Keypoints", num_active_keypoints, counters[phase])


                    if phase == 'train' and global_iteration % config['train']['ckp_per_iter'] == 0:
                        save_model(model_dy, '%s/net_dy_epoch_%d_iter_%d' % (train_dir, epoch, i))


                log = '%s [%d/%d] Loss: %.6f, Best valid: %.6f' % (
                    phase, epoch, config['train']['n_epoch'], average_meter_container[valid_loss_type].avg, best_valid_loss)
                print(log)

                # record all average_meter losses
                for key, meter in average_meter_container.items():
                    writer.add_scalar("AvgMeter/%s/%s" %(key, phase), meter.avg, epoch)

                if phase == "train":
                    if (scheduler is not None) and (config['train']['lr_scheduler']['type'] == "StepLR"):
                        scheduler.step()

                if phase == 'valid':

                    # print the weight matrix
                    weight_matrix = model_dy.weight_matrix
                    print("weight_matrix\n", weight_matrix)

                    if (scheduler is not None) and (config['train']['lr_scheduler']['type'] == "ReduceLROnPlateau"):
                        scheduler.step(average_meter_container[valid_loss_type].avg)

                    if average_meter_container[valid_loss_type].avg < best_valid_loss:
                        best_valid_loss = average_meter_container[valid_loss_type].avg
                        training_stats['epoch'] = epoch
                        training_stats['global_iteration'] = counters['valid']
                        save_yaml(training_stats, training_stats_file)
                        save_model(model_dy, '%s/net_best_dy' % (train_dir))

                writer.flush() # flush SummaryWriter events to disk

    except KeyboardInterrupt:
        # save network if we have a keyboard interrupt
        save_model(model_dy, '%s/net_dy_epoch_%d_keyboard_interrupt' % (train_dir, epoch_counter_external))
        writer.flush() # flush SummaryWriter events to disk
