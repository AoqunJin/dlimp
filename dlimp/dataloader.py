from typing import Dict, Sequence
import tensorflow as tf
from functools import partial
import dlimp as dl

# using 51 bits for trajectory index encoding allows for datasets of size 2^51 trajectories (over 2 quadrillion),
# leaving 12 bits for the trajectory length which allows for trajectories of length 4096
_TRAJ_IDX_ENCODING_BITS = 51


def make_dataset(
    path: str,
    *,
    seed: int = 0,
    batch_size: int,
    shuffle_buffer_size: int = 25000,
    frame_transforms: Sequence[dl.transforms.Transform] = (
        dl.transforms.unflatten_dict,
        dl.transforms.decode_images,
        "cache",
    ),
    traj_transforms: Sequence[dl.transforms.Transform] = (dl.transforms.add_next_obs,),
    traj_transforms_first: bool = False,
) -> tf.data.Dataset:
    """Create a tf.data.Dataset from a directory of tfrecord files.

    The dataset is customizable by passing in a sequence of transforms to apply at the frame and trajectory level. A
    transform is a function that operates on a dictionary of tensors, or the string "cache" to cache the dataset in
    memory. Be careful when using "cache" as it will freeze any randomness from previous transforms.

    Args:
        path (str): Path to a directory containing tfrecord files. seed (int, optional): Random seed. Defaults to 0.
        seed (int, optional): Random seed for shuffling.
        batch_size (int): Batch size. shuffle_buffer_size (int, optional): Size of the shuffle buffer. Defaults to
            25000. Set to 1 to disable shuffling.
        frame_transforms (Sequence[Transform], optional): A sequence of transforms to apply at the frame level. A
            transform is a function that operates on a dictionary of tensors, or the string "cache" to cache the dataset
            in memory.
        traj_transforms (Sequence[Transform], optional): A sequence of functions to apply at the trajectory level. A
            transform is a function that operates on a dictionary of tensors, or the string "cache" to cache the dataset
            in memory.
        traj_transforms_first (bool, optional): Whether to apply the trajectory transforms before the frame transforms.
    """
    # get the tfrecord files
    paths = tf.io.gfile.glob(tf.io.gfile.join(path, "*.tfrecord"))

    if shuffle_buffer_size > 1:
        paths = tf.random.shuffle(paths, seed=seed)

    # extract the type spec from the first file
    type_spec = _get_type_spec(paths[0])

    # read the tfrecords (yields raw serialized examples)
    dataset = tf.data.TFRecordDataset(paths, num_parallel_reads=tf.data.AUTOTUNE)

    options = tf.data.Options()
    options.autotune.enabled = True
    options.experimental_optimization.apply_default_optimizations = True
    options.experimental_optimization.map_fusion = True
    dataset = dataset.with_options(options)

    # decode the examples (yields trajectories)
    dataset = dataset.map(
        partial(_decode_example, type_spec=type_spec),
        num_parallel_calls=tf.data.AUTOTUNE,
        deterministic=True,
    )

    # add a unique index and length metadata to each trajectory for the purpose of regrouping them later
    dataset = dataset.enumerate().map(_add_traj_metadata, num_parallel_calls=tf.data.AUTOTUNE, deterministic=True)

    if traj_transforms_first:
        # apply trajectory transforms
        dataset = dl.transforms.apply_transforms(dataset, traj_transforms, deterministic=False)

    # unbatch to get individual frames
    dataset = dataset.unbatch()

    # apply frame transforms
    dataset = dl.transforms.apply_transforms(dataset, frame_transforms, deterministic=not traj_transforms_first)

    if not traj_transforms_first:
        # regroup the frames into trajectories
        dataset = dataset.group_by_window(
            key_func=lambda x: tf.cast(x["_len"], tf.int64) * (2**_TRAJ_IDX_ENCODING_BITS) + x["_i"],
            reduce_func=lambda k, d: d.batch(k // (2**_TRAJ_IDX_ENCODING_BITS), num_parallel_calls=tf.data.AUTOTUNE),
            window_size_func=lambda k: k // (2**_TRAJ_IDX_ENCODING_BITS),
        )

        # apply trajectory transforms
        dataset = dl.transforms.apply_transforms(dataset, traj_transforms, deterministic=False)

        # unbatch to get individual frames again
        dataset = dataset.unbatch()

    # shuffle the dataset
    if shuffle_buffer_size > 1:
        dataset = dataset.shuffle(shuffle_buffer_size, seed=seed)

    dataset = dataset.repeat()

    # batch the dataset
    dataset = dataset.batch(batch_size, num_parallel_calls=tf.data.AUTOTUNE)

    # always prefetch last
    dataset = dataset.prefetch(tf.data.AUTOTUNE)

    return dataset


def _decode_example(example_proto: tf.Tensor, type_spec: Dict[str, tf.TensorSpec]) -> Dict[str, tf.Tensor]:
    features = {key: tf.io.FixedLenFeature([], tf.string) for key in type_spec.keys()}
    parsed_features = tf.io.parse_single_example(example_proto, features)
    parsed_tensors = {key: tf.io.parse_tensor(parsed_features[key], spec.dtype) for key, spec in type_spec.items()}

    for key in parsed_tensors:
        parsed_tensors[key] = tf.ensure_shape(parsed_tensors[key], type_spec[key].shape)

    return parsed_tensors


def _get_type_spec(path: str) -> Dict[str, tf.TensorSpec]:
    """Get a type spec from a tfrecord file.

    Args:
        path (str): Path to a single tfrecord file.

    Returns:
        dict: A dictionary mapping feature names to tf.TensorSpecs.
    """
    data = next(iter(tf.data.TFRecordDataset(path))).numpy()
    example = tf.train.Example()
    example.ParseFromString(data)

    out = {}
    for key, value in example.features.feature.items():
        data = value.bytes_list.value[0]
        tensor_proto = tf.make_tensor_proto([])
        tensor_proto.ParseFromString(data)
        dtype = tf.dtypes.as_dtype(tensor_proto.dtype)
        shape = [d.size for d in tensor_proto.tensor_shape.dim]
        if shape:
            shape[0] = None  # first dimension is trajectory length, which is variable
        out[key] = tf.TensorSpec(shape=shape, dtype=dtype)

    return out


def _add_traj_metadata(i: tf.Tensor, x: Dict[str, tf.Tensor]) -> Dict[str, tf.Tensor]:
    # get the length of each dict entry
    traj_lens = {k: tf.shape(v)[0] if len(v.shape) > 0 else None for k, v in x.items()}

    # take the maximum length as the canonical length (elements should either be the same length or length 1)
    traj_len = tf.reduce_max([l for l in traj_lens.values() if l is not None])

    for k in x:
        # broadcast scalars to the length of the trajectory
        if traj_lens[k] is None:
            x[k] = tf.repeat(x[k], traj_len)
            traj_lens[k] = traj_len

        # broadcast length-1 elements to the length of the trajectory
        if traj_lens[k] == 1:
            x[k] = tf.repeat(x[k], traj_len, axis=0)
            traj_lens[k] = traj_len

    k = tf.cast(traj_len, tf.int64) * (2**_TRAJ_IDX_ENCODING_BITS) + i
    asserts = [
        # make sure all the lengths are the same
        tf.assert_equal(tf.size(tf.unique(tf.stack(list(traj_lens.values()))).y), 1),
        # make sure the key encoding is working
        tf.assert_equal(k % (2**_TRAJ_IDX_ENCODING_BITS), i),
        tf.assert_equal(k // (2**_TRAJ_IDX_ENCODING_BITS), tf.cast(traj_len, tf.int64)),
    ]

    assert "_i" not in x
    assert "_len" not in x
    x["_i"] = tf.repeat(i, traj_len)
    x["_len"] = tf.repeat(traj_len, traj_len)

    with tf.control_dependencies(asserts):
        return x
