"""
Training using the bags
"""

import numpy as np
import matplotlib.pyplot as plt
from model_learning import (
    TrajDataset,
    train_model,
    eval_model,
    numpy_collate,
    save_checkpoint,
    restore_checkpoint,
)
import ruamel.yaml as yaml
import torch.utils.data as data
from flax.training import train_state
import optax
import jax
from mlp import MLP

# import tf
import transforms3d.euler as euler
from itertools import accumulate

gamma = 1


def compute_traj(sim_data, rho=1, horizon=300, full_state=False):
    # TODO: full state

    # get the reference trajectory
    # col W-Y position
    ref_traj_x = sim_data[:, 22]
    ref_traj_y = sim_data[:, 23]
    ref_traj_z = sim_data[:, 24]
    # col AL yaw_des
    ref_traj_yaw = sim_data[:, 37]
    ref_traj = np.vstack((ref_traj_x, ref_traj_y, ref_traj_z, ref_traj_yaw)).T
    # debug: print the first 10 ref_traj
    print("ref_traj: ", ref_traj[:10, :])

    # get the actual trajectory
    # col C-E position
    actual_traj_x = sim_data[:, 2]
    actual_traj_y = sim_data[:, 3]
    actual_traj_z = sim_data[:, 4]
    # col I-L quaternion
    actual_traj_quat = sim_data[:, 8:12]
    # (cur_roll, cur_pitch, cur_yaw) = tf.transformations.euler_from_quaternion(actual_traj_quat)
    # 4 element sequence: w, x, y, z of quaternion
    # print("actual_traj_quat's shape: ", actual_traj_quat.shape)
    actual_yaw = np.zeros(len(actual_traj_quat))
    (cur_roll, cur_pitch, cur_yaw) = (
        np.zeros(len(actual_traj_quat)),
        np.zeros(len(actual_traj_quat)),
        np.zeros(len(actual_traj_quat)),
    )
    for i in range(len(actual_traj_quat)):
        (cur_roll[i], cur_pitch[i], cur_yaw[i]) = euler.quat2euler(actual_traj_quat[i])
        actual_yaw[i] = cur_yaw[i]
    actual_traj = np.vstack((actual_traj_x, actual_traj_y, actual_traj_z, actual_yaw)).T
    # debug: print the first 10 actual_traj
    print("actual_traj: ", actual_traj[:10, :])
    # print("actual_traj's type: ", type(actual_traj))

    # get the cmd input
    # col BN desired thrust from so3 controller
    input_traj_thrust = sim_data[:, 65]
    # print("input_traj_thrust's shape: ", input_traj_thrust.shape)

    # get the angular velocity from odometry: col M-O
    odom_ang_vel = sim_data[:, 12:15]

    """
    # coverting quaternion to euler angle and get the angular velocity
    # col BO-BR desired orientation from so3 controller(quaternion)
    input_traj_quat = sim_data[:, 66:70]
    # (cmd_roll, cmd_pitch, cmd_yaw) = tf.transformations.euler_from_quaternion(input_traj_quat)
    input_traj_yaw = np.zeros(len(input_traj_quat))
    (cmd_roll, cmd_pitch, cmd_yaw) = (
        np.zeros(len(input_traj_quat)),
        np.zeros(len(input_traj_quat)),
        np.zeros(len(input_traj_quat)),
    )
    for i in range(len(input_traj_quat)):
        (cmd_roll[i], cmd_pitch[i], cmd_yaw[i]) = euler.quat2euler(input_traj_quat[i])

    # devided by time difference to get the angular velocity x, y, z
    input_traj_ang_vel = (
        np.diff(np.vstack((cmd_roll, cmd_pitch, cmd_yaw)).T, axis=0) / 0.01
    )
    # add the first element to the first row, so that the shape is the same as input_traj_thrust
    input_traj_ang_vel = np.vstack((input_traj_ang_vel[0, :], input_traj_ang_vel))
    # print("input_traj_ang_vel's shape: ", input_traj_ang_vel.shape)
    
    input_traj = np.hstack((input_traj_thrust.reshape(-1, 1), input_traj_ang_vel))
    """

    input_traj = np.hstack((input_traj_thrust.reshape(-1, 1), odom_ang_vel))

    # debug: print the first 10 input_traj
    print("input_traj: ", input_traj[:10, :])

    # get the cost
    cost_traj = compute_cum_tracking_cost(
        ref_traj, actual_traj, input_traj, horizon, horizon, rho
    )
    # debug: print the first 10 cost_traj
    print("cost_traj: ", cost_traj[:10, :])

    return ref_traj, actual_traj, input_traj, cost_traj, sim_data[:, 0]


def compute_cum_tracking_cost(ref_traj, actual_traj, input_traj, horizon, N, rho):
    # print type of input
    print("ref_traj's type: ", type(ref_traj))
    print("actual_traj's type: ", type(actual_traj))
    print("input_traj's type: ", type(input_traj))

    import ipdb

    # ipdb.set_trace()

    m, n = ref_traj.shape
    num_traj = int(m / horizon)
    xcost = []
    for i in range(num_traj):
        act = actual_traj[i * horizon : (i + 1) * horizon, :]
        act = np.append(act, act[-1, :] * np.ones((N - 1, n)))
        act = np.reshape(act, (horizon + N - 1, n))
        r0 = ref_traj[i * horizon : (i + 1) * horizon, :]
        r0 = np.append(r0, r0[-1, :] * np.ones((N - 1, n)))
        r0 = np.reshape(r0, (horizon + N - 1, n))

        xcost.append(
            rho
            * (
                np.linalg.norm(act[:, :3] - r0[:, :3], axis=1) ** 2
                + angle_wrap(act[:, 3] - r0[:, 3]) ** 2
            )
            + 0.1 * np.linalg.norm(input_traj[i]) ** 2
        )

    xcost.reverse()
    cost = []
    for i in range(num_traj):
        tot = list(accumulate(xcost[i], lambda x, y: x * gamma + y))
        cost.append(np.log(tot[-1]))
    cost.reverse()
    return np.vstack(cost)


def angle_wrap(theta):
    return (theta + np.pi) % (2 * np.pi) - np.pi


def main():
    horizon = 300
    rho = 100
    gamma = 1
    # Load bag
    # sim_data = load_bag('/home/anusha/2022-09-27-11-49-40.bag')
    # sim_data = load_bag("/home/anusha/2023-02-27-13-35-15.bag")
    # sim_data = load_bag("/home/anusha/dragonfly1-2023-04-12-12-18-27.bag")
    ### Load the csv file here with header
    sim_data = np.loadtxt(
        "/home/mrsl_guest/Desktop/dragonfly1.csv", delimiter=",", skiprows=1
    )

    # no need times
    ref_traj, actual_traj, input_traj, cost_traj, times = compute_traj(sim_data)

    with open(
        # r"/home/anusha/Research/ws_kr/src/layered_ref_control/src/layered_ref_control/data/params.yaml"
        r"/home/mrsl_guest/hanli_ws/src/new_layered_ref_control/src/layered_ref_control/data/params.yaml"
    ) as f:
        yaml_data = yaml.load(f, Loader=yaml.RoundTripLoader)

    num_hidden = yaml_data["num_hidden"]
    batch_size = yaml_data["batch_size"]
    learning_rate = yaml_data["learning_rate"]
    num_epochs = yaml_data["num_epochs"]
    model_save = yaml_data["save_path"]
    #  + str(rho)
    # Construct augmented states

    cost_traj = cost_traj.ravel()

    print("Costs", cost_traj)

    num_traj = int(len(ref_traj) / horizon)

    # Create augmented state

    aug_state = []
    for i in range(num_traj):
        r0 = ref_traj[i * horizon : (i + 1) * horizon, :]
        act = actual_traj[i * horizon : (i + 1) * horizon, :]
        aug_state.append(np.append(act[0, :], r0))

    aug_state = np.array(aug_state)
    print(aug_state.shape)

    Tstart = 0
    Tend = aug_state.shape[0]

    train_dataset = TrajDataset(
        aug_state[Tstart : Tend - 1, :].astype("float64"),
        input_traj[Tstart : Tend - 1, :].astype("float64"),
        cost_traj[Tstart : Tend - 1, None].astype("float64"),
        aug_state[Tstart + 1 : Tend, :].astype("float64"),
    )

    p = aug_state.shape[1]
    q = 4

    print(aug_state.shape)

    model = MLP(num_hidden=num_hidden, num_outputs=1)
    # Printing the model shows its attributes
    print(model)

    rng = jax.random.PRNGKey(427)
    rng, inp_rng, init_rng = jax.random.split(rng, 3)
    inp = jax.random.normal(inp_rng, (batch_size, p))  # Batch size 64, input size p
    # Initialize the model
    params = model.init(init_rng, inp)

    optimizer = optax.sgd(learning_rate=learning_rate, momentum=0.9)

    model_state = train_state.TrainState.create(
        apply_fn=model.apply, params=params, tx=optimizer
    )

    train_data_loader = data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, collate_fn=numpy_collate
    )
    trained_model_state = train_model(
        model_state, train_data_loader, num_epochs=num_epochs
    )
    """
    # Train on 2nd dataset
    sim_data = load_bag("/home/anusha/dragonfly2-2023-04-12-12-18-27.bag")

    ref_traj, actual_traj, input_traj, cost_traj, times = compute_traj(
        sim_data, "dragonfly2", "/home/anusha/min_jerk_times.pkl", rho
    )
    sim_data.close()

    # Construct augmented states

    cost_traj = cost_traj.ravel()

    print("Costs", cost_traj)

    num_traj = int(len(ref_traj) / horizon)

    # Create augmented state

    aug_state = []
    for i in range(num_traj):
        r0 = ref_traj[i * horizon : (i + 1) * horizon, :]
        act = actual_traj[i * horizon : (i + 1) * horizon, :]
        aug_state.append(np.append(act[0, :], r0))

    aug_state = np.array(aug_state)
    print(aug_state.shape)

    Tstart = 0
    Tend = aug_state.shape[0]

    train_dataset = TrajDataset(
        aug_state[Tstart : Tend - 1, :].astype("float64"),
        input_traj[Tstart : Tend - 1, :].astype("float64"),
        cost_traj[Tstart : Tend - 1, None].astype("float64"),
        aug_state[Tstart + 1 : Tend, :].astype("float64"),
    )

    p = aug_state.shape[1]
    q = 4

    print(aug_state.shape)

    train_data_loader = data.DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, collate_fn=numpy_collate
    )
    trained_model_state = train_model(
        trained_model_state, train_data_loader, num_epochs=num_epochs
    )
    """
    # Evaluation of network

    eval_model(trained_model_state, train_data_loader, batch_size)

    trained_model = model.bind(trained_model_state.params)
    # TODO: save checkpoint
    # save_checkpoint(trained_model_state, model_save, 7)

    # Save plot on entire test dataset
    out = []
    true = []
    for batch in train_data_loader:
        data_input, _, cost, _ = batch
        out.append(trained_model(data_input))
        true.append(cost)

    out = np.vstack(out)
    true = np.vstack(true)

    plt.figure()
    plt.plot(out.ravel(), "b-", label="Predictions")
    plt.plot(true.ravel(), "r--", label="Actual")
    plt.legend()
    plt.title("Predictions of the trained network for different rho")
    # plt.savefig("./plots/inference"+str(rho)+".png")
    plt.show()

    """
    # Inference on bag2

    # inf_data = load_bag('/home/anusha/rho01.bag')
    # inf_data = load_bag("/home/anusha/IROS_bags/2023-02-27-13-35-15.bag")
    inf_data = load_bag("/home/anusha/dragonfly2-2023-04-12-12-18-27.bag")

    ref_traj, actual_traj, input_traj, cost_traj, times = compute_traj(
        inf_data, "dragonfly2", "/home/anusha/min_jerk_times.pkl", rho
    )
    inf_data.close()

    # Construct augmented states
    horizon = 300
    gamma = 1

    idx = [0, 1, 2, 12]

    cost_traj = cost_traj.ravel()

    num_traj = int(len(ref_traj) / horizon)

    # Create augmented state

    aug_state = []
    for i in range(num_traj):
        r0 = ref_traj[i * horizon : (i + 1) * horizon, :]
        act = actual_traj[i * horizon : (i + 1) * horizon, :]
        aug_state.append(np.append(act[0, :], r0))

    aug_state = np.array(aug_state)
    print(aug_state.shape)

    Tstart = 0
    Tend = aug_state.shape[0]

    test_dataset = TrajDataset(
        aug_state[Tstart : Tend - 1, :].astype("float64"),
        input_traj[Tstart : Tend - 1, :].astype("float64"),
        cost_traj[Tstart : Tend - 1, None].astype("float64"),
        aug_state[Tstart + 1 : Tend, :].astype("float64"),
    )

    test_data_loader = data.DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False, collate_fn=numpy_collate
    )
    eval_model(trained_model_state, test_data_loader, batch_size)

    # Save plot on entire test dataset
    out = []
    true = []
    for batch in test_data_loader:
        data_input, _, cost, _ = batch
        out.append(trained_model(data_input))
        true.append(cost)

    out = np.vstack(out)
    true = np.vstack(true)

    plt.figure()
    plt.plot(out.ravel(), "b-", label="Predictions")
    plt.plot(true.ravel(), "r--", label="Actual")
    plt.legend()
    plt.title("Predictions of the trained network for different rho")
    # plt.savefig("./plots/inference"+str(rho)+".png")
    plt.show()
    """


if __name__ == "__main__":
    main()