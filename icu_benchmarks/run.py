# -*- coding: utf-8 -*-
from datetime import datetime

import gin
import logging
import sys
import torch
import wandb
from pathlib import Path
from icu_benchmarks.imputation import name_mapping

import importlib.util

from icu_benchmarks.tuning.hyperparameters import choose_and_bind_hyperparameters
from scripts.plotting.utils import plot_aggregated_results
from icu_benchmarks.cross_validation import execute_repeated_cv
from icu_benchmarks.run_utils import (
    build_parser,
    create_run_dir,
    aggregate_results,
    log_full_line,
)

@gin.configurable("Run")
def get_mode(mode: gin.REQUIRED):
    return mode

def main(my_args=tuple(sys.argv[1:])):
    args, _ = build_parser().parse_known_args(my_args)
    if args.wandb_sweep:
        wandb.init()
        sweep_config = wandb.config
        args.__dict__.update(sweep_config)
        for key, value in sweep_config.items():
            args.hyperparams.append(f"{key}=" + (('\'' + value + '\'') if isinstance(value, str) else str(value)))

    debug = args.debug
    cache = args.cache
    if debug and cache:
        raise ValueError("Caching is not supported in debug mode.")

    log_fmt = "%(asctime)s - %(levelname)s: %(message)s"
    logging.basicConfig(format=log_fmt)
    logging.getLogger().setLevel(logging.INFO)


    load_weights = args.command == "evaluate"
    args.data_dir = Path(args.data_dir)
    name = args.name
    if name is None:
        name = args.data_dir.name
    task = args.task
    model = args.model
    if isinstance(args.seed, int):
        args.seed = [args.seed]

    gin.parse_config_file(f"configs/tasks/{task}.gin")
    mode = get_mode()
    logging.info(f"Task mode: {mode}")
    experiment = args.experiment
    
    if args.use_pretrained_imputation is not None and not Path(args.use_pretrained_imputation).exists():
        logging.info("the specified pretrained imputation model does not exist")
        args.use_pretrained_imputation = None
    
    if args.use_pretrained_imputation is not None:
        logging.info("using pretrained imputation from" + str(args.use_pretrained_imputation))
        pretrained_imputation_model_checkpoint = torch.load(args.use_pretrained_imputation, map_location=torch.device('cpu'))
        # check if the loaded model is a pytorch lightning checkpoint
        # if isinstance(pretrained_imputation_model, dict):
        #     model_name = Path(args.use_pretrained_imputation).parent.parent.parent.name
        #     model_class = name_mapping[model_name]
        #     pretrained_imputation_model = model_class.load_from_checkpoint(args.use_pretrained_imputation)
        imputation_model_class = pretrained_imputation_model_checkpoint["class"]
        pretrained_imputation_model = imputation_model_class(**pretrained_imputation_model_checkpoint["hyper_parameters"])
        pretrained_imputation_model.load_state_dict(pretrained_imputation_model_checkpoint["state_dict"])
        pretrained_imputation_model = pretrained_imputation_model.to("cuda" if torch.cuda.is_available() else "cpu")
    else:
        pretrained_imputation_model = None

    if wandb.run is not None:
        print("updating wandb config:", {"pretrained_imputation_model": pretrained_imputation_model.__class__.__name__ if pretrained_imputation_model is not None else "None"})
        wandb.config.update({"pretrained_imputation_model": pretrained_imputation_model.__class__.__name__ if pretrained_imputation_model is not None else "None"})
    source_dir = None
    reproducible = False
    log_dir_name = args.log_dir / name
    log_dir = (log_dir_name / experiment) if experiment else (log_dir_name / args.task_name / model)

    if args.preprocessor:
        # Import custom supplied preprocessor
        try:
            spec = importlib.util.spec_from_file_location("CustomPreprocessor", args.preprocessor)
            module = importlib.util.module_from_spec(spec)
            sys.modules["preprocessor"] = module
            spec.loader.exec_module(module)
            gin.bind_parameter("preprocess.preprocessor", module.CustomPreprocessor)
        except Exception as e:
            logging.error(f"Could not import custom preprocessor from {args.preprocessor}: {e}")

    if load_weights:
        log_dir /= f"from_{args.source_name}"
        run_dir = create_run_dir(log_dir)
        source_dir = args.source_dir
        gin.parse_config_file(source_dir / "train_config.gin")
    else:
        reproducible = args.reproducible
        checkpoint = log_dir / args.checkpoint if args.checkpoint else None
        model_path = Path("configs") / ("imputation_models" if mode == "Imputation" else "classification_models") / f"{model}.gin"
        gin_config_files = (
            [Path(f"configs/experiments/{args.experiment}.gin")]
            if args.experiment
            else [model_path, Path(f"configs/tasks/{task}.gin")]
        )
        gin.parse_config_files_and_bindings(gin_config_files, args.hyperparams, finalize_config=False)
        run_dir = create_run_dir(log_dir)
        choose_and_bind_hyperparameters(args.tune, args.data_dir, run_dir, args.seed, checkpoint=checkpoint, debug=args.debug)

    logging.info(f"Logging to {run_dir.resolve()}")
    log_full_line("STARTING TRAINING", level=logging.INFO, char="=", num_newlines=3)
    start_time = datetime.now()
    execute_repeated_cv(
        args.data_dir,
        run_dir,
        args.seed,
        load_weights=load_weights,
        source_dir=source_dir,
        reproducible=reproducible,
        debug=args.debug,
        load_cache=args.load_cache,
        generate_cache=args.generate_cache,
        mode=mode,
        pretrained_imputation_model=pretrained_imputation_model,
        dataset_name=name,
    )

    log_full_line("FINISHED TRAINING", level=logging.INFO, char="=", num_newlines=3)
    execution_time = datetime.now() - start_time
    log_full_line(f"DURATION: {execution_time}", level=logging.INFO, char="")
    aggregate_results(run_dir, execution_time)
    if args.plot:
        plot_aggregated_results(run_dir, "aggregated_test_metrics.json")


"""Main module."""
if __name__ == "__main__":
    main()
