import argparse
import gzip
import logging
import math
import numpy as np
import os
import pandas as pd

import time
import torch
import json
import pickle
from unifold.config import model_config
from unifold.modules.alphafold import AlphaFold
from unifold.data import residue_constants, protein
from unifold.dataset import load_and_process, UnifoldDataset
from unicore.utils import (
    tensor_tree_map,
)
from unifold.data.data_ops import get_pairwise_distances
from unifold.data import residue_constants as rc

from alphafold.relax import relax

# from https://github.com/deepmind/alphafold/blob/main/run_alphafold.py

RELAX_MAX_ITERATIONS = 0
RELAX_ENERGY_TOLERANCE = 2.39
RELAX_STIFFNESS = 10.0
RELAX_EXCLUDE_RESIDUES = []
RELAX_MAX_OUTER_ITERATIONS = 3


def get_device_mem(device):
    if device != "cpu" and torch.cuda.is_available():
        cur_device = torch.cuda.current_device()
        prop = torch.cuda.get_device_properties("cuda:{}".format(cur_device))
        total_memory_in_GB = prop.total_memory / 1024 / 1024 / 1024
        return total_memory_in_GB
    else:
        return 40

def automatic_chunk_size(seq_len, device, is_bf16):
    total_mem_in_GB = get_device_mem(device)
    factor = math.sqrt(total_mem_in_GB/40.0*(0.55 * is_bf16 + 0.45))*0.95
    if seq_len < int(1024*factor):
        chunk_size = 256
        block_size = None
    elif seq_len < int(2048*factor):
        chunk_size = 128
        block_size = None
    elif seq_len < int(3072*factor):
        chunk_size = 64
        block_size = None
    elif seq_len < int(4096*factor):
        chunk_size = 32
        block_size = 512
    else:
        chunk_size = 4
        block_size = 256
    return chunk_size, block_size

def load_feature_for_one_target(
    config, data_folder, crosslinks, seed=0, is_multimer=False, use_uniprot=False, neff=-1, dropout_crosslinks=-1,
):
    if not is_multimer:
        uniprot_msa_dir = None
        sequence_ids = ["A"]
        if use_uniprot:
            uniprot_msa_dir = data_folder

    else:
        uniprot_msa_dir = data_folder
        sequence_ids = open(os.path.join(data_folder, "chains.txt")).readline().split() # A B C?
    batch, _ = load_and_process(
        config=config.data,
        mode="predict",
        seed=seed,
        batch_idx=None,
        data_idx=0,
        is_distillation=False,
        sequence_ids=sequence_ids,
        monomer_feature_dir=data_folder,
        uniprot_msa_dir=uniprot_msa_dir,
        is_monomer=(not is_multimer),
        crosslinks=crosslinks,
        neff=neff,
        dropout_crosslinks=dropout_crosslinks,
    )
    batch = UnifoldDataset.collater([batch])
    return batch
def load_rdc_file(path):
    """Load a tab-delimited RDC file with columns: residue, atom1, atom2, value."""
    try:
        df = pd.read_csv(path, sep="\t", header=None, names=["residue", "atom1", "atom2", "value"])
        return df
    except Exception as e:
        raise ValueError(f"Failed to read RDC file: {path}\n{e}")

def main(args):
    rdc_df = None
    use_rdcs = args.rdc_path is not None
    if use_rdcs:
        try:
            print(f"Loading RDCs from {args.rdc_path}")
            rdc_df = load_rdc_file(args.rdc_path)
        except Exception as e:
            raise ValueError(f"Failed to read RDC file: {args.rdc_path}\n{e}")
    config = model_config(args.model_name)
    config.data.common.max_recycling_iters = args.max_recycling_iters
    config.globals.max_recycling_iters = args.max_recycling_iters
    config.data.predict.num_ensembles = args.num_ensembles
    is_multimer = config.model.is_multimer
    if args.sample_templates:
        # enable template samples for diversity
        config.data.predict.subsample_templates = True
    model = AlphaFold(config)

    print("start to load params {}".format(args.param_path))
    state_dict = torch.load(args.param_path, weights_only=False)["ema"]["params"]
    state_dict = {".".join(k.split(".")[1:]): v for k, v in state_dict.items()}
    model.load_state_dict(state_dict)
    model = model.to(args.model_device)
    model.eval()
    model.inference_mode()
    if args.bf16:
        model.bfloat16()

    # data path is based on target_name
    data_dir = args.data_dir #os.path.join(args.data_dir, args.target_name)
    output_dir = args.output_dir #os.path.join(args.output_dir, args.target_name)
    os.system("mkdir -p {}".format(output_dir))
    cur_param_path_postfix = os.path.split(args.param_path)[-1]
    name_postfix = ""
    if args.sample_templates:
        name_postfix += "_st"
    if not is_multimer and args.use_uniprot:
        name_postfix += "_uni"
    if args.max_recycling_iters != 3:
        name_postfix += "_r" + str(args.max_recycling_iters)
    if args.num_ensembles != 2:
        name_postfix += "_e" + str(args.num_ensembles)

    print("start to predict {}".format(args.target_name))
    plddts = {}
    ptms = {}

    cur_seed = hash((args.data_random_seed, 0)) % 100000

    seed = 0
    best_out = None
    best_iptm = 0.0
    best_seed = None

    for it in range(args.times):
        cur_seed = hash((args.data_random_seed, seed)) % 100000

        batch = load_feature_for_one_target(
            config,
            data_dir,
            args.crosslinks,
            cur_seed,
            is_multimer=is_multimer,
            use_uniprot=args.use_uniprot,
            neff=args.neff,
            dropout_crosslinks=args.dropout_crosslinks,
        )

        seed += 1
        seq_len = batch["aatype"].shape[-1]
        # faster prediction with large chunk/block size
        chunk_size, block_size = automatic_chunk_size(
                                    seq_len,
                                    args.model_device,
                                    args.bf16
                                )
        model.globals.chunk_size = chunk_size
        model.globals.block_size = block_size

        with torch.no_grad():
            batch = {
                k: torch.as_tensor(v, device=args.model_device)
                for k, v in batch.items()
            }
            shapes = {k: v.shape for k, v in batch.items()}
            # print(shapes)
            t = time.perf_counter()
            raw_out = model(batch)
            print(f"Inference time: {time.perf_counter() - t}")

        def to_float(x):
            if x.dtype == torch.bfloat16 or x.dtype == torch.half:
                return x.float()
            else:
                return x

        if not args.save_raw_output:
            score = ["plddt", "ptm", "iptm", "iptm+ptm"]
            out = {
                    k: v for k, v in raw_out.items()
                    if k.startswith("final_") or k in score
                }
        else:
            out = raw_out
        del raw_out
        # Toss out the recycling dimensions --- we don't need them anymore
        batch = tensor_tree_map(lambda t: t[-1, 0, ...], batch)
        batch = tensor_tree_map(to_float, batch)
        out = tensor_tree_map(lambda t: t[0, ...], out)
        out = tensor_tree_map(to_float, out)
        batch = tensor_tree_map(lambda x: np.array(x.cpu()), batch)
        out = tensor_tree_map(lambda x: np.array(x.cpu()), out)

        ca_idx = rc.atom_order["CA"]
        ca_coords = torch.from_numpy(out["final_atom_positions"][..., ca_idx, :])
        
        if rdc_df is not None:
            # Use RDC Q-factor as selection metric
            def compute_q_factor(pred_coords, rdc_df):
                # Simplified: assumes atom1/atom2 are always N/H or other valid single-atom types
                coords_np = pred_coords.detach().cpu().numpy()
                diffs = []
                values = []
                for _, row in rdc_df.iterrows():
                    res_idx = int(row["residue"]) - 1  # assuming 1-based indexing in file
                    atom1 = row["atom1"]
                    atom2 = row["atom2"]
                    try:
                        a1_idx = rc.atom_order[atom1]
                        a2_idx = rc.atom_order[atom2]
                        vec = coords_np[res_idx, a2_idx] - coords_np[res_idx, a1_idx]
                        angle = vec[2] / np.linalg.norm(vec)  # assume alignment along z
                        pred_rdc = angle**2  # simple dipolar model (scaled out)
                        diffs.append(pred_rdc - row["value"])
                        values.append(row["value"])
                    except Exception as e:
                        continue  # skip invalid atoms or missing values
                diffs = np.array(diffs)
                values = np.array(values)
                if len(values) == 0:
                    return np.inf  # prevent selecting if no RDCs match
                q = np.sqrt(np.mean(diffs**2)) / np.sqrt(np.mean(values**2))
                return q
        
            q_score = compute_q_factor(out["final_atom_positions"][..., :], rdc_df)
            print(f"Model {it} RDC Q factor: {q_score:.4f} Model confidence: {np.mean(out['iptm+ptm']):.3f}")
        
            if best_out is None or q_score < best_q_score:
                best_q_score = q_score
                best_out = out
                best_seed = cur_seed
        
        else:
            distances = get_pairwise_distances(ca_coords)
            xl = torch.from_numpy(batch['xl'][...,0] > 0)
            interface = torch.from_numpy(batch['asym_id'][..., None] != batch['asym_id'][..., None, :])
            satisfied = torch.sum(distances[xl & interface] <= args.cutoff) / 2
            total_xl = torch.sum(xl & interface) / 2
            print("Model %d Crosslink satisfaction: %.3f Model confidence: %.3f" % (it, satisfied / total_xl, np.mean(out["iptm+ptm"])))
        
            if best_out is None or np.mean(out["iptm+ptm"]) > best_iptm:
                best_iptm = np.mean(out["iptm+ptm"])
                best_out = out
                best_seed = cur_seed
        



#         distances = get_pairwise_distances(ca_coords)#[0]#[0,0]
# 	# We may be add the RDCs here as a selection criteria
        
#         xl = torch.from_numpy(batch['xl'][...,0] > 0)
        
#         interface = torch.from_numpy(batch['asym_id'][..., None] != batch['asym_id'][..., None, :])

#         satisfied = torch.sum(distances[xl & interface] <= args.cutoff) / 2

#         total_xl = torch.sum(xl & interface) / 2

#         if np.mean(out["iptm+ptm"]) > best_iptm:
#             best_iptm = np.mean(out["iptm+ptm"])
#             best_out = out
#             best_seed = cur_seed

#         print("Model %d Crosslink satisfaction: %.3f Model confidence: %.3f" %(it,satisfied / total_xl, np.mean(out["iptm+ptm"])))

        plddt = out["plddt"]
        mean_plddt = np.mean(plddt)
        plddt_b_factors = np.repeat(
            plddt[..., None], residue_constants.atom_type_num, axis=-1
        )
        cur_protein = protein.from_prediction(
            features=batch, result=out, b_factors=plddt_b_factors*100
        )

        iptm_str = np.mean(out["iptm+ptm"])

        cur_save_name = (
            f"AlphaLink2_{cur_seed}_{iptm_str:.3f}.pdb"
        )

        with open(os.path.join(output_dir, cur_save_name), "w") as f:
            f.write(protein.to_pdb(cur_protein))


        if args.save_raw_output:
            with gzip.open(os.path.join(output_dir, cur_save_name + '_outputs.pkl.gz'), 'wb') as f:
                pickle.dump(out, f)
        # del out


    out = best_out

    plddt = out["plddt"]
    mean_plddt = np.mean(plddt)
    plddt_b_factors = np.repeat(
        plddt[..., None], residue_constants.atom_type_num, axis=-1
    )
    # TODO: , may need to reorder chains, based on entity_ids
    cur_protein = protein.from_prediction(
        features=batch, result=out, b_factors=plddt_b_factors
    )

    iptm_str = np.mean(out["iptm+ptm"])
    cur_save_name = (
        f"AlphaLink2_{cur_param_path_postfix}_{best_seed}_{iptm_str:.3f}"
    )
    plddts[cur_save_name] = str(mean_plddt)
    if is_multimer:
        ptms[cur_save_name] = str(np.mean(out["iptm+ptm"]))

    if args.relax:
        amber_relaxer = relax.AmberRelaxation(
            max_iterations=RELAX_MAX_ITERATIONS,
            tolerance=RELAX_ENERGY_TOLERANCE,
            stiffness=RELAX_STIFFNESS,
            exclude_residues=RELAX_EXCLUDE_RESIDUES,
            max_outer_iterations=RELAX_MAX_OUTER_ITERATIONS,
            use_gpu=True)

        relaxed_pdb_str, _, violations = amber_relaxer.process(
            prot=cur_protein)


        with open(os.path.join(output_dir, cur_save_name + '_best.pdb'), "w") as f:
            f.write(relaxed_pdb_str)


    print("plddts", plddts)
    score_name = f"{args.model_name}_{cur_param_path_postfix}_{args.data_random_seed}_{args.times}{name_postfix}"
    plddt_fname = score_name + "_plddt.json"
    json.dump(plddts, open(os.path.join(output_dir, plddt_fname), "w"), indent=4)
    if ptms:
        print("ptms", ptms)
        ptm_fname = score_name + "_ptm.json"
        json.dump(ptms, open(os.path.join(output_dir, ptm_fname), "w"), indent=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model_device",
        type=str,
        default="cuda:0",
        help="""Name of the device on which to run the model. Any valid torch
             device name is accepted (e.g. "cpu", "cuda:0")""",
    )
    parser.add_argument(
        "--model_name",
        type=str,
        default="model_5_ptm_af2",
    )
    parser.add_argument(
        "--param_path", type=str, default=None, help="Path to model parameters."
    )
    parser.add_argument(
        "--data_random_seed",
        type=int,
        default=42,
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        default="",
    )
    parser.add_argument(
        "--crosslinks",
        type=str,
        default="",
    )
    parser.add_argument(
        "--neff",
        type=int,
        default=-1,
        help="Downsample MSAs to given Neff",
    )
    parser.add_argument(
        "--dropout_crosslinks",
        type=int,
        default=-1,
        help="Remove MSAs at crosslinked positions. True for all positive arguments.",
    )
    parser.add_argument(
        "--target_name",
        type=str,
        default="",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="",
    )
    parser.add_argument(
        "--times",
        type=int,
        default=25,
    )
    parser.add_argument(
        "--max_recycling_iters",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--num_ensembles",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--cutoff",
        type=float,
        default=25,
    )
    parser.add_argument("--sample_templates", action="store_true")
    parser.add_argument("--use_uniprot", action="store_true")
    parser.add_argument("--relax", action="store_true")
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument("--save_raw_output", action="store_true")
    parser.add_argument(
    "--rdc_path",
    type=str,
    default=None,
    help="Optional path to tab-delimited RDC file with columns: residue, atom1, atom2, value"
)
    args = parser.parse_args()

    if args.model_device == "cpu" and torch.cuda.is_available():
        logging.warning(
            """The model is being run on CPU. Consider specifying
            --model_device for better performance"""
        )

    main(args)
