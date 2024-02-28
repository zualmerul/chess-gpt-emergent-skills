import einops
import torch
from tqdm import tqdm
import logging
import itertools

import train_test_chess
from train_test_chess import Config, LinearProbeData

torch.set_grad_enabled(False)

# Flags to control logging
debug_mode = False
info_mode = True

if debug_mode:
    log_level = logging.DEBUG
elif info_mode:
    log_level = logging.INFO
else:
    log_level = logging.WARNING

# Configure logging
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

BATCH_SIZE = 1
MAXIMUM_TRAINING_GAMES = 500


def check_tensor_values(tensor, tensor_name="Tensor"):
    """Check if a tensor contains NaN, inf, or -inf values because we are summing 30k+ activations together."""
    # isneginf is currently not implemented for mps tensors
    original_device_type = tensor.device.type
    if original_device_type == "mps":
        tensor = tensor.cpu()

    if torch.any(torch.isinf(tensor)):
        raise ValueError(f"Overflow detected: {tensor_name} contains inf")
    if torch.any(torch.isneginf(tensor)):
        raise ValueError(f"Overflow detected: {tensor_name} contains -inf")
    if torch.any(torch.isnan(tensor)):
        raise ValueError(f"Invalid value detected: {tensor_name} contains NaN")

    if original_device_type == "mps":
        tensor = tensor.to("mps")


@torch.no_grad()
def create_contrastive_activations(
    activation_name: str,
    probe_data: LinearProbeData,
    config: Config,
    logging_dict: dict,
):
    assert logging_dict["split"] == "train", "Don't train on the test set"

    num_games = (MAXIMUM_TRAINING_GAMES // BATCH_SIZE) * BATCH_SIZE

    if num_games < len(probe_data.board_seqs_int):
        raise ValueError(
            f"Number of games ({num_games}) is less than the number of games in the dataset ({len(probe_data.board_seqs_int)})"
        )

    current_iter = 0
    full_train_indices = torch.arange(0, num_games)
    sum_high_elo = torch.zeros((512), device=device)
    sum_low_elo = torch.zeros((512), device=device)
    count_high_elo = 0
    count_low_elo = 0
    for i in tqdm(range(0, num_games, BATCH_SIZE)):
        indices = full_train_indices[i : i + BATCH_SIZE]
        games_int = probe_data.board_seqs_int[indices]  # shape (batch, pgn_str_length)
        games_dots = probe_data.custom_indices[indices]  # shape (batch, num_white_moves)
        games_dots = games_dots[:, config.pos_start :]

        if config.probing_for_skill:
            games_skill = probe_data.skill_stack[indices]
            logger.debug(f"games_skill shape: {games_skill.shape}")
        else:
            raise Exception("CAA currently only supports skill vectors")

        _, cache = probe_data.model.run_with_cache(games_int.to(device)[:, :-1], return_type=None)
        resid_post = cache["resid_post", layer][:, :]  # shape (batch, pgn_str_length - 1, d_model)

        indexed_resid_posts = []

        for batch_idx in range(games_dots.size(0)):
            # Get the indices for the current batch
            dots_indices_for_batch = games_dots[batch_idx]

            # Index the state_stack for the current batch
            indexed_resid_post = resid_post[batch_idx, dots_indices_for_batch]

            # Append the result to the list
            indexed_resid_posts.append(indexed_resid_post)

        # Stack the indexed state stacks along the first dimension
        # This results in a tensor of shape [2, 61, 8, 8] (assuming all batches have 61 indices)
        resid_post = torch.stack(indexed_resid_posts)  # shape (batch, num_white_moves, d_model)
        summed_resid_post = einops.reduce(
            resid_post, "batch indices model_dim -> batch model_dim", "sum"
        )  # shape (batch, d_model)

        for batch_idx in range(BATCH_SIZE):
            if games_skill[batch_idx] == config.levels_of_interest[1]:
                sum_high_elo += summed_resid_post[batch_idx]  # shape (d_model)
                count_high_elo += 1
            elif games_skill[batch_idx] == config.levels_of_interest[0]:
                sum_low_elo += summed_resid_post[batch_idx]  # shape (d_model)
                count_low_elo += 1
            else:
                raise Exception("Invalid skill level")

        logger.debug(
            f"count_high_elo: {count_high_elo}, count_low_elo: {count_low_elo}, games_skill: {games_skill}"
        )

        if i % 100 == 0:
            logger.info(
                f"batch {i}, count_high_elo: {count_high_elo}, count_low_elo: {count_low_elo}"
            )

        current_iter += BATCH_SIZE

    check_tensor_values(sum_high_elo, "sum_high_elo")
    check_tensor_values(sum_low_elo, "sum_low_elo")

    average_high_elo_activation = sum_high_elo / count_high_elo  # shape (d_model)
    average_low_elo_activation = sum_low_elo / count_low_elo  # shape (d_model)

    difference_vector = average_high_elo_activation - average_low_elo_activation

    logging_dict["average_high_elo_activation"] = average_high_elo_activation
    logging_dict["average_low_elo_activation"] = average_low_elo_activation
    logging_dict["difference_vector"] = difference_vector
    logging_dict["count_high_elo"] = count_high_elo
    logging_dict["count_low_elo"] = count_low_elo

    output_location = f"{CAA_DIR}{activation_name}.pt"

    logger.info(f"Saving activations to {output_location}")
    torch.save(logging_dict, output_location)


MODEL_DIR = "models/"
DATA_DIR = "data/"
CAA_DIR = "contrastive_activations/"

device = (
    "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
)
logger.info(f"Using device: {device}")

config = train_test_chess.skill_config

# Sweep over layers, levels of interest, pos_start, and dataset_prefix

layers = range(11, 15, 1)
levels_of_interest = [[0, 5]]
pos_starts = [25]

caa_type = "simple"
caa_type = "cascade"

cascade_layers = ""

if caa_type == "cascade":
    cascade_layers = [f"{str(layer)}_" for layer in layers]

for (
    layer,
    level,
    pos_start,
) in itertools.product(layers, levels_of_interest, pos_starts):
    dataset_prefix = "lichess_"
    layer = layer
    split = "train"
    n_layers = 16
    model_name = f"tf_lens_{dataset_prefix}{n_layers}layers_ckpt_no_optimizer"
    config.levels_of_interest = level
    input_dataframe_file = f"{DATA_DIR}{dataset_prefix}{split}.csv"
    config = train_test_chess.set_config_min_max_vals_and_column_name(
        config, input_dataframe_file, dataset_prefix
    )
    config.pos_start = pos_start

    probe_data = train_test_chess.construct_linear_probe_data(
        input_dataframe_file,
        layer,
        dataset_prefix,
        n_layers,
        model_name,
        config,
        MAXIMUM_TRAINING_GAMES,
        device,
    )

    levels_str = "".join([str(i) for i in level])

    activation_name = (
        f"type=caa_{caa_type}{cascade_layers}_model={n_layers}layers_layer={layer}_activations"
    )

    logging_dict = train_test_chess.init_logging_dict(
        config, probe_data, split, dataset_prefix, model_name, n_layers
    )

    create_contrastive_activations(activation_name, probe_data, config, logging_dict)
