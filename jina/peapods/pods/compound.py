import copy
from argparse import Namespace
from itertools import cycle
from typing import Optional, Dict, List, Union, Set
from contextlib import ExitStack

from .. import BasePod
from .. import Pea
from .. import Pod
from ..networking import get_connect_host
from ... import helper
from ...enums import SocketType, SchedulerType
from ...helper import random_identity


class CompoundPod(BasePod, ExitStack):
    """A CompoundPod is a immutable set of pods, which run in parallel.
    A CompoundPod is an abstraction using a composable pattern to abstract the usage of parallel Pods that act as shards.

    CompoundPod will make sure to add a `HeadPea` and a `TailPea` to serve as routing/merging pattern for the different Pod shards

    :param args: pod arguments parsed from the CLI. These arguments will be used for each of the shards
    :param needs: pod names of preceding pods, the output of these pods are going into the input of this pod
    """

    head_args = None
    tail_args = None

    def __init__(
        self, args: Union['Namespace', Dict], needs: Optional[Set[str]] = None
    ):
        super().__init__()
        args.upload_files = BasePod._set_upload_files(args)
        self.args = args
        self.needs = (
            needs or set()
        )  #: used in the :class:`jina.flow.Flow` to build the graph
        # we will see how to have `CompoundPods` in remote later when we add tests for it
        self.is_head_router = True
        self.is_tail_router = True
        self.head_args = BasePod._copy_to_head_args(args, args.polling)
        self.update_tail_args()
        self.assign_replicas()

    def assign_replicas(self):
        """Assign replicas to the CompoundPod"""
        cargs = copy.copy(self.args)
        self.shards = []  # type: List['Pod']
        self.shards_args = self._set_shard_args(
            cargs, self.head_args, self.tail_args
        )
        for _args in self.shards_args:
            if getattr(self.args, 'noblock_on_start', False):
                _args.noblock_on_start = True
            self.shards.append(Pod(_args))

    def update_tail_args(self):
        """
        Sets the tail args based on the current args
        """
        self.tail_args = BasePod._copy_to_tail_args(self.args, self.args.polling)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        super().__exit__(exc_type, exc_val, exc_tb)
        self.join()

    @property
    def port_jinad(self) -> int:
        """Get the JinaD remote port

        .. # noqa: DAR201
        """
        return self.head_args.port_jinad

    @property
    def host(self) -> str:
        """Get the host name of this Pod

        .. # noqa: DAR201
        """
        return self.head_args.host

    def _parse_pod_args(self, args: Namespace) -> List[Namespace]:
        return self._set_shard_args(
            args,
            head_args=self.head_args,
            tail_args=self.tail_args,
        )

    @property
    def num_peas(self) -> int:
        """
        Get the number of running :class:`Pod`

        :return: total number of peas including head and tail
        """
        return sum([shard.num_peas for shard in self.shards]) + 2

    def __eq__(self, other: 'CompoundPod'):
        return self.num_peas == other.num_peas and self.name == other.name

    def _enter_pea(self, pea: 'Pea') -> None:
        self.enter_context(pea)

    def start(self) -> 'CompoundPod':
        """
        Start to run all :class:`Pod` and :class:`Pea` in this CompoundPod.

        :return: started CompoundPod

        .. note::
            If one of the :class:`Pod` fails to start, make sure that all of them
            are properly closed.
        """
        if getattr(self.args, 'noblock_on_start', False):
            head_args = self.head_args
            head_args.noblock_on_start = True
            self.head_pea = Pea(head_args)
            self._enter_pea(self.head_pea)
            for shard in self.shards:
                self._enter_replica(shard)
            tail_args = self.tail_args
            tail_args.noblock_on_start = True
            self.tail_pea = Pea(tail_args)
            self._enter_pea(self.tail_pea)
        else:
            try:
                head_args = self.head_args
                self.head_pea = Pea(head_args)
                self._enter_pea(self.head_pea)
                for shard in self.shards:
                    self._enter_replica(shard)
                tail_args = self.tail_args
                self.tail_pea = Pea(tail_args)
                self._enter_pea(self.tail_pea)
            except:
                self.close()
                raise
            return self

    def wait_start_success(self) -> None:
        """
        Block until all pods and peas start successfully.
        If not successful, it will raise an error hoping the outer function to catch it
        """

        if not self.args.noblock_on_start:
            raise ValueError(
                f'{self.wait_start_success!r} should only be called when `noblock_on_start` is set to True'
            )

        try:
            self.head_pea.wait_start_success()
            self.tail_pea.wait_start_success()
            for p in self.shards:
                p.wait_start_success()
        except:
            self.close()
            raise

    def _enter_replica(self, shard: 'Pod') -> None:
        self.enter_context(shard)

    def join(self):
        """Wait until all pods and peas exit."""
        try:
            if getattr(self, 'head_pea', None):
                self.head_pea.join()
            if getattr(self, 'tail_pea', None):
                self.tail_pea.join()
            for p in self.shards:
                p.join()
        except KeyboardInterrupt:
            pass
        finally:
            self.shards.clear()

    @property
    def is_ready(self) -> bool:
        """
        Checks if Pod is read.
        :return: true if the peas and pods are ready to serve requests

        .. note::
            A Pod is ready when all the Peas it contains are ready
        """
        return all(
            [p.is_ready.is_set() for p in [self.head_pea, self.tail_pea]]
            + [p.is_ready for p in self.shards]
        )

    @staticmethod
    def _set_shard_args(
        args: Namespace,
        head_args: Namespace,
        tail_args: Namespace,
    ) -> List[Namespace]:
        """
        Sets the arguments of the shards in the compound pod.

        :param args: arguments configured by the user for the shards
        :param head_args: head args from the compound pod
        :param tail_args: tail args from the compound pod

        :return: list of arguments for the shards
        """
        result = []
        _host_list = (
            args.peas_hosts
            if args.peas_hosts
            else [
                args.host,
            ]
        )
        host_generator = cycle(_host_list)
        for idx in range(args.shards):
            _args = copy.deepcopy(args)
            pod_host_list = [
                host for _, host in zip(range(args.replicas), host_generator)
            ]
            _args.peas_hosts = pod_host_list
            _args.shard_id = idx
            _args.identity = random_identity()
            if _args.name:
                _args.name += f'/shard-{idx}'
            else:
                _args.name = f'{idx}'

            _args.port_in = head_args.port_out
            _args.port_out = tail_args.port_in
            _args.port_ctrl = helper.random_port()

            if args.polling.is_push:
                if args.scheduling == SchedulerType.ROUND_ROBIN:
                    _args.socket_in = SocketType.PULL_CONNECT
                elif args.scheduling == SchedulerType.LOAD_BALANCE:
                    _args.socket_in = SocketType.DEALER_CONNECT
                else:
                    raise ValueError(
                        f'{args.scheduling} is not supported as a SchedulerType!'
                    )
            else:
                _args.socket_in = SocketType.SUB_CONNECT

            _args.socket_out = SocketType.PUSH_CONNECT

            _args.dynamic_routing = False
            # ugly trick to avoid Head of Replica to have wrong host in
            tmp_args = copy.deepcopy(_args)
            if _args.replicas > 1:
                tmp_args.runs_in_docker = False
                tmp_args.uses = ''

            _args.host_in = get_connect_host(
                bind_host=head_args.host,
                bind_expose_public=head_args.expose_public,
                connect_args=tmp_args,
            )
            _args.host_out = get_connect_host(
                bind_host=tail_args.host,
                bind_expose_public=tail_args.expose_public,
                connect_args=tmp_args,
            )
            result.append(_args)
        return result

    def rolling_update(self, dump_path: Optional[str] = None):
        """Reload all Pods of this Compound Pod.

        :param dump_path: the dump from which to read the data
        """
        try:
            for i in range(len(self.shards)):
                shard = self.shards[i]
                shard.close()
                _args = self.shards_args[i]
                _args.noblock_on_start = False
                _args.dump_path = dump_path
                new_shard = Pod(_args)
                self.enter_context(new_shard)
                self.shards[i] = new_shard
        except:
            raise

    @property
    def _mermaid_str(self) -> List[str]:
        """String that will be used to represent the Pod graphically when `Flow.plot()` is invoked


        .. # noqa: DAR201
        """
        mermaid_graph = [f'subgraph {self.name};\n', f'direction LR;\n']
        head_name = self.head_args.name
        tail_name = self.tail_args.name
        pod_names = []
        for replica in self.replicas:
            pod_names.append(replica.name)
            replica_mermaid_graph = replica._mermaid_str
            replica_mermaid_graph = [
                node.replace(';', '\n') for node in replica_mermaid_graph
            ]
            mermaid_graph.extend(replica_mermaid_graph)
            mermaid_graph.append('\n')

        for name in pod_names:
            mermaid_graph.append(f'{head_name}:::HEADTAIL --> {name};')
            mermaid_graph.append(f'{name} --> {tail_name}:::HEADTAIL;')
        mermaid_graph.append('end;')

        return mermaid_graph
