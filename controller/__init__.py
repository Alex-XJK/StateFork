from .base_env_manager import EnvironmentManager
from .container_env_manager import ContainerAttachManager, ContainerBuildManager
from .criu_env_manager import CRIUAttachManager, CRIUBuildManager
from .hybrid_env_manager import HybridAttachManager, HybridBuildManager
from .waypoint_env_manager import WaypointAttachManager, WaypointBuildManager
from .gvisor_env_manager import GvisorBuildManager, GvisorAttachManager
from .firecracker_env_manager import FireBuildManager, FireAttachManager
from .benchmark import BenchmarkStats, BenchmarkResult, Statistics
from decider.decider import Decider, RandomDecider, AlwaysFalseDecider, AlwaysTrueDecider

from typing import Literal
from pathlib import Path
import psutil

EnvType = Literal[
    "criu_build", "criu_attach",
    "docker_build", "docker_attach",
    "podman_build", "podman_attach",
    "hybrid_build", "hybrid_attach",
    "waypoint_build", "waypoint_attach", "ckpt_build", "ckpt_attach",
    "gvisor_build", "gvisor_attach",
    "firecracker_build", "firecracker_attach"
]

"""
Apply the Factory Method pattern to create different environment managers based on the method type.
"""
def create_env_manager(method: EnvType, **kwargs) -> EnvironmentManager:
    if method == "criu_build":
        return CRIUBuildManager(
            work_dir=kwargs.get("work_dir", "/tmp/statefork_criu"),
            command=kwargs.get("command"),
            decider=kwargs.get("decider")
        )
    elif method == "criu_attach":
        return CRIUAttachManager(
            target_pid=kwargs["target_pid"],
            work_dir=kwargs.get("work_dir", "/tmp/statefork_criu"),
            decider=kwargs.get("decider")
        )
    elif method == "docker_build":
        return ContainerBuildManager(
            backend="Docker",
            base_image=kwargs.get("base_image"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "docker_attach":
        return ContainerAttachManager(
            backend="Docker",
            container_name=kwargs["container_name"],
            base_image=kwargs.get("base_image", "statefork-app:base"),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "podman_build":
        return ContainerBuildManager(
            backend="Podman",
            base_image=kwargs.get("base_image"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "podman_attach":
        return ContainerAttachManager(
            backend="Podman",
            container_name=kwargs["container_name"],
            base_image=kwargs.get("base_image", "statefork-app:base"),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "hybrid_build":
        return HybridBuildManager(
            container_name=kwargs.get("container_name", "podman-build"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            export_dir=kwargs.get("export_dir", "/tmp/statefork_podman"),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "hybrid_attach":
        return HybridAttachManager(
            container_name=kwargs["container_name"],
            export_dir=kwargs.get("export_dir", "/tmp/statefork_podman"),
            decider=kwargs.get("decider")
        )
    elif method in ("waypoint_build", "ckpt_build"):
        return WaypointBuildManager(
            dockerfile_dir=kwargs.get("dockerfile_dir"),
            build=kwargs.get("build", True),
            decider=kwargs.get("decider")
        )
    elif method in ("waypoint_attach", "ckpt_attach"):
        return WaypointAttachManager(
            session_id=kwargs["session_id"],
            target_pid=kwargs.get("target_pid", -2),
            decider=kwargs.get("decider")
        )
    elif method == "gvisor_build":
        return GvisorBuildManager(
            base_image=kwargs.get("base_image"),
            dockerfile_dir=kwargs.get("dockerfile_dir", "."),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "gvisor_attach":
        return GvisorAttachManager(
            container_name=kwargs["container_name"],
            base_image=kwargs.get("base_image", "statefork-app:base"),
            extra_args=kwargs.get("extra_args"),
            decider=kwargs.get("decider")
        )
    elif method == "firecracker_build":
        return FireBuildManager(
            fire_parent_dir=kwargs.get("firecracker_dir", "/tmp"), # create artifact and ckpt directories here
            inject_dir=kwargs.get("inject_dir", "app"), # pass files to be in the vm
            decider=kwargs.get("decider")
        )
    elif method == "firecracker_attach":
        fire_pid = int(kwargs["pid"])
        return FireAttachManager(
            fire_process = psutil.Process(fire_pid),
            microvm_ip=kwargs.get("microvm_ip", "172.16.0.2"),
            tap_dev=kwargs.get("tap_dev", "tap0"),

            key=Path(kwargs["key"]),
            checkpoint_dir=Path(kwargs.get("checkpoint_dir", "/tmp/fire_ckpts")),
            vm_dir=Path(kwargs.get("vm_dir", "/tmp/fire_vm")),
            fire_binary=Path(kwargs.get("fire_binary", "/tmp/fire_vm/firecracker")),

            api_socket=kwargs.get("api_socket", "/tmp/firecracker.socket"),
            decider=kwargs.get("decider")
        )
    else:
        raise ValueError(f"Unknown method: {method}")
