#!/usr/bin/env python3
"""为 Bash 教学 lab 提供 NetworkManager 命令入口。"""

import argparse
from pathlib import Path

from network_manager import FileLeaseStore, NetworkFailure, NetworkManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "get", "activate", "release"))
    parser.add_argument("--lease-path", type=Path, required=True)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gateway-port", type=int, default=18082)
    args = parser.parse_args()
    instance_id = "microvm-lab"
    store = FileLeaseStore(args.lease_path)
    manager = NetworkManager(store, allow_host_connections=True)

    if args.action == "prepare":
        if args.uid is None:
            parser.error("prepare requires --uid")
        # 调用者已确认 lab 停止，先回收上次退出留下的租约和网络。
        manager.release(instance_id)
        lease = manager.allocate(instance_id)
        manager.setup(lease, args.uid, (args.gateway_port,))
    elif args.action == "release":
        manager.release(instance_id)
        return
    elif args.action == "activate":
        manager.activate(instance_id)
        return
    else:
        lease = store.get(instance_id)
        if lease is None:
            raise NetworkFailure("laboratory network lease does not exist")

    # 固定字段顺序供 Bash read 使用，不生成或执行 shell 代码。
    print("\t".join(str(lease[key]) for key in (
        "tap", "host_cidr", "guest_ip", "guest_cidr", "host_ip",
        "guest_mac", "subnet", "nft_table",
    )))


if __name__ == "__main__":
    main()
