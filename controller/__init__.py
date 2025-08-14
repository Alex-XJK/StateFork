from .base_env_manager import EnvironmentManager
from .container_env_manager import ContainerAttachManager, ContainerBuildManager
from .criu_env_manager import CRIUAttachManager, CRIUBuildManager
from .hybrid_env_manager import HybridAttachManager, HybridBuildManager
from .ckptlite_env_manager import CheckpointLiteAttachManager, CheckpointLiteBuildManager
from .benchmark import BenchmarkStats, BenchmarkResult, Statistics

from typing import Literal

EnvType = Literal[
    "criu_build", "criu_attach",
    "docker_build", "docker_attach",
    "podman_build", "podman_attach",
    "hybrid_build", "hybrid_attach",
    "ckpt_build", "ckpt_attach"
]

"""
Apply the Factory Method pattern to create different environment managers based on the method type.
"""
def create_env_manager(method: EnvType, **kwargs) -> EnvironmentManager:
    if method == "criu_build":
        return CRIUBuildManager(
            work_dir=kwargs.get("work_dir", "/tmp/statefork_criu"),
            command=kwargs.get("command")
        )
    elif method == "criu_attach":
        return CRIUAttachManager(
            target_pid=kwargs["target_pid"],
            work_dir=kwargs.get("work_dir", "/tmp/statefork_criu")
        )
    elif method == "docker_build":
        return ContainerBuildManager(
            backend="Docker",
            base_image=kwargs.get("base_image"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            extra_args=kwargs.get("extra_args")
        )
    elif method == "docker_attach":
        return ContainerAttachManager(
            backend="Docker",
            container_name=kwargs["container_name"],
            base_image=kwargs.get("base_image", "statefork-app:base"),
            extra_args=kwargs.get("extra_args")
        )
    elif method == "podman_build":
        return ContainerBuildManager(
            backend="Podman",
            base_image=kwargs.get("base_image"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            extra_args=kwargs.get("extra_args")
        )
    elif method == "podman_attach":
        return ContainerAttachManager(
            backend="Podman",
            container_name=kwargs["container_name"],
            base_image=kwargs.get("base_image", "statefork-app:base"),
            extra_args=kwargs.get("extra_args")
        )
    elif method == "hybrid_build":
        return HybridBuildManager(
            container_name=kwargs.get("container_name", "podman-build"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            export_dir=kwargs.get("export_dir", "/tmp/statefork_podman"),
            extra_args=kwargs.get("extra_args")
        )
    elif method == "hybrid_attach":
        return HybridAttachManager(
            container_name=kwargs["container_name"],
            export_dir=kwargs.get("export_dir", "/tmp/statefork_podman")
        )
    elif method == "ckpt_build":
        return CheckpointLiteBuildManager(
            init_dir=kwargs.get("init_dir"),
            command=kwargs.get("command", "default")
        )
    elif method == "ckpt_attach":
        return CheckpointLiteAttachManager(
            target_pid=kwargs["target_pid"],
            session_id=kwargs["session_id"]
        )
    else:
        raise ValueError(f"Unknown method: {method}")
