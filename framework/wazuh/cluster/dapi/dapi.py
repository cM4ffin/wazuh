# Copyright (C) 2015-2019, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is a free software; you can redistribute it and/or modify it under the terms of GPLv2
import asyncio
import itertools
import json
import operator
import random
from importlib import import_module
from typing import Callable, Dict, Union, Tuple
from wazuh.cluster import local_client, cluster, common as c_common
from wazuh import exception, agent, common, utils
from wazuh import Wazuh
import logging
import time


class DistributedAPI:
    """
    Represents a distributed API request
    """
    def __init__(self, f: Callable, logger: logging.Logger, f_kwargs: Dict = {}, node: c_common.Handler = None,
                 debug: bool = False, pretty: bool = False, request_type: str = "local_master",
                 wait_for_complete: bool = False, from_cluster: bool = False, is_async: bool = False):
        """
        Class constructor

        :param input_json: JSON containing information/arguments about the request.
        :param logger: Logging logger to use
        :param node: Asyncio protocol object to use when sending requests to other nodes
        :param debug: Enable debug messages and raise exceptions.
        :param pretty: Return request result with pretty indent
        """
        self.logger = logger
        self.f = f
        self.f_kwargs = f_kwargs
        self.node = node if node is not None else local_client
        self.cluster_items = cluster.get_cluster_items() if node is None else node.cluster_items
        self.debug = debug
        self.pretty = pretty
        self.node_info = cluster.get_node()
        self.request_id = str(random.randint(0, 2**10 - 1))
        self.request_type = request_type
        self.wait_for_complete = wait_for_complete
        self.from_cluster = from_cluster
        self.is_async = is_async

    async def distribute_function(self) -> str:
        """
        Distributes an API call

        :return: Dictionary with API response
        """
        try:
            is_dapi_enabled = cluster.get_cluster_items()['distributed_api']['enabled']

            # First case: execute the request local.
            # If the distributed api is not enabled
            # If the cluster is disabled or the request type is local_any
            # if the request was made in the master node and the request type is local_master
            # if the request came forwarded from the master node and its type is distributed_master
            if not is_dapi_enabled or cluster.check_cluster_status() or self.request_type == 'local_any' or \
                    (self.request_type == 'local_master' and self.node_info['type'] == 'master') or \
                    (self.request_type == 'distributed_master' and self.from_cluster):

                return await self.execute_local_request()

            # Second case: forward the request
            # Only the master node will forward a request, and it will only be forwarded if its type is distributed_
            # master
            elif self.request_type == 'distributed_master' and self.node_info['type'] == 'master':
                return await self.forward_request()

            # Last case: execute the request remotely.
            # A request will only be executed remotely if it was made in a worker node and its type isn't local_any
            else:
                return await self.execute_remote_request()
        except exception.WazuhException as e:
            if self.debug:
                raise
            return self.print_json(data=e.message, error=e.code)
        except Exception as e:
            if self.debug:
                raise
            return self.print_json(data=str(e), error=1000)

    def print_json(self, data: Union[Dict, str], error: int = 0) -> str:
        def encode_json(o):
            try:
                return getattr(o, 'to_dict')()
            except AttributeError as e:
                self.print_json(error=1000, data="Wazuh-Python Internal Error: data encoding unknown ({})".format(e))

        output = {'message' if error else 'data': data, 'error': error}
        #return json.dumps(obj=output, default=encode_json, indent=4 if self.pretty else None)
        return output

    async def execute_local_request(self) -> str:
        """
        Executes an API request locally.

        :return: a JSON response.
        """
        def run_local():
            self.logger.debug("Starting to execute request locally")
            data = self.f(**self.f_kwargs)
            self.logger.debug("Finished executing request locally")
            return data
        try:
            before = time.time()

            timeout = None if self.wait_for_complete \
                           else self.cluster_items['intervals']['communication']['timeout_api_request']

            if self.is_async:
                task = run_local()
            else:
                loop = asyncio.get_running_loop()
                task = loop.run_in_executor(None, run_local)

            try:
                data = await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                raise exception.WazuhException(3021)

            after = time.time()
            self.logger.debug("Time calculating request result: {}s".format(after - before))
            return self.print_json(data=data, error=0)
        except exception.WazuhException as e:
            if self.debug:
                raise
            return self.print_json(data=e.message, error=e.code)
        except Exception as e:
            self.logger.error("Error executing API request locally: {}".format(e))
            if self.debug:
                raise
            return self.print_json(data=str(e), error=1000)

    def to_dict(self):
        return {"f": self.f,
                "f_kwargs": self.f_kwargs,
                "request_type": self.request_type,
                "wait_for_complete": self.wait_for_complete,
                "from_cluster": self.from_cluster,
                "is_async": self.is_async
                }

    async def execute_remote_request(self) -> str:
        """
        Executes a remote request. This function is used by worker nodes to execute master_only API requests.

        :return: JSON response
        """
        return await self.node.execute(command=b'dapi', data=json.dumps(self.to_dict(), cls=CallableEncoder).encode(),
                                       wait_for_complete=self.wait_for_complete)

    async def forward_request(self):
        """
        Forwards a request to the node who has all available information to answer it. This function is called when a
        distributed_master function is used. Only the master node calls this function. An API request will only be
        forwarded to worker nodes.

        :return: a JSON response.
        """
        async def forward(node_name: Tuple) -> str:
            """
            Forwards a request to a node.
            :param node_name: Node to forward a request to.
            :return: a JSON response
            """
            node_name, agent_id = node_name
            if agent_id and ('agent_id' not in self.f_kwargs or isinstance(self.f_kwargs['agent_id'], list)):
                self.f_kwargs['agent_id'] = agent_id
            if node_name == 'unknown' or node_name == '' or node_name == self.node_info['node']:
                # The request will be executed locally if the the node to forward to is unknown, empty or the master
                # itself
                response = await self.distribute_function()
            else:
                response = json.loads(await self.node.execute(b'dapi_forward',
                                                              "{} {}".format(node_name,
                                                                             json.dumps(self.to_dict(),
                                                                                        cls=CallableEncoder)
                                                                             ).encode(),
                                                              self.wait_for_complete))
            return response

        # get the node(s) who has all available information to answer the request.
        nodes = self.get_solver_node()
        self.from_cluster = True
        if len(nodes) > 1:
            results = await asyncio.shield(asyncio.gather(*[forward(node) for node in nodes.items()]))
            final_json = {}
            response = self.merge_results(results, final_json)
        else:
            response = await forward(next(iter(nodes.items())))
        return response

    def get_solver_node(self) -> Dict:
        """
        Gets the node(s) that can solve a request, the node(s) that has all the necessary information to answer it.
        Only called when the request type is 'master_distributed' and the node_type is master.

        :return: node name and whether the result is list or not
        """
        select_node = {'fields': ['node_name']}
        if 'agent_id' in self.f_kwargs:
            # the request is for multiple agents
            if isinstance(self.f_kwargs['agent_id'], list):
                agents = agent.Agent.get_agents_overview(select=select_node, limit=None,
                                                         filters={'id': self.f_kwargs['agent_id']},
                                                         sort={'fields': ['node'], 'order': 'desc'})['items']
                node_name = {k: list(map(operator.itemgetter('id'), g)) for k, g in
                             itertools.groupby(agents, key=operator.itemgetter('node_name'))}

                # add non existing ids in the master's dictionary entry
                non_existent_ids = list(set(self.f_kwargs['agent_id']) -
                                        set(map(operator.itemgetter('id'), agents)))
                if non_existent_ids:
                    if self.node_info['node'] in node_name:
                        node_name[self.node_info['node']].extend(non_existent_ids)
                    else:
                        node_name[self.node_info['node']] = non_existent_ids

                return node_name
            # if the request is only for one agent
            else:
                # Get the node where the agent 'agent_id' is reporting
                node_name = agent.Agent.get_agent(self.f_kwargs['agent_id'],
                                                  select=select_node)['node_name']
                return {node_name: [self.f_kwargs['agent_id']]}

        elif 'node_id' in self.f_kwargs:
            node_id = self.f_kwargs['node_id']
            del self.f_kwargs['node_id']
            return {node_id: []}

        else:  # agents, syscheck, rootcheck and syscollector
            # API calls that affect all agents. For example, PUT/agents/restart, DELETE/rootcheck, etc...
            agents = agent.Agent.get_agents_overview(select=select_node, limit=None,
                                                     sort={'fields': ['node_name'], 'order': 'desc'})['items']
            node_name = {k: [] for k, _ in itertools.groupby(agents, key=operator.itemgetter('node_name'))}
            return node_name

    def merge_results(self, responses, final_json):
        """
        Merge results from an API call.
        To do the merging process, the following is considered:
            1.- If the field is a list, append items to it
            2.- If the field is a message (msg), only replace it if the new message has more priority.
            3.- If the field is a integer:
                * if it's totalItems, sum
                * if it's an error, only replace it if its value is higher
        The priorities are defined in a list of tuples. The first item of the tuple is the element which has more priority.
        :param responses: list of results from each node
        :param final_json: JSON to return.
        :return: single JSON with the final result
        """
        priorities = {
            ("Some agents were not restarted", "All selected agents were restarted")
        }

        for local_json in responses:
            for key, field in local_json.items():
                field_type = type(field)
                if field_type == dict:
                    final_json[key] = self.merge_results([field], {} if key not in final_json else final_json[key])
                elif field_type == list:
                    if key in final_json:
                        final_json[key].extend([elem for elem in field if elem not in final_json[key]])
                    else:
                        final_json[key] = field
                elif field_type == int:
                    if key in final_json:
                        if key == 'totalItems':
                            final_json[key] += field
                        elif key == 'error' and final_json[key] < field:
                            final_json[key] = field
                    else:
                        final_json[key] = field
                else:  # str
                    if key in final_json:
                        if (field, final_json[key]) in priorities:
                            final_json[key] = field
                    else:
                        final_json[key] = field

        if 'data' in final_json and 'items' in final_json['data'] and isinstance(final_json['data']['items'], list):
            if 'offset' not in self.f_kwargs:
                self.f_kwargs['offset'] = 0
            if 'limit' not in self.f_kwargs:
                self.f_kwargs['limit'] = common.database_limit

            if 'sort' in self.f_kwargs:
                final_json['data']['items'] = utils.sort_array(final_json['data']['items'],
                                                               self.f_kwargs['sort']['fields'],
                                                               self.f_kwargs['sort']['order'])

            offset, limit = self.f_kwargs['offset'], self.f_kwargs['limit']
            final_json['data']['items'] = final_json['data']['items'][offset:offset+limit]

        return final_json


class APIRequestQueue:
    """
    Represents a queue of API requests. This thread will be always in background, it will remain blocked until a
    request is pushed into its request_queue. Then, it will answer the request and get blocked again.
    """
    def __init__(self, server):
        self.request_queue = asyncio.Queue()
        self.server = server
        self.logger = logging.getLogger('wazuh').getChild('dapi')
        self.logger.addFilter(cluster.ClusterFilter(tag='Cluster', subtag='D API'))
        self.pending_requests = {}

    async def run(self):
        while True:
            # name    -> node name the request must be sent to. None if called from a worker node.
            # id      -> id of the request.
            # request -> JSON containing request's necessary information
            names, request = (await self.request_queue.get()).split(' ', 1)
            names = names.split('*', 1)
            name_2 = '' if len(names) == 1 else names[1] + ' '
            node = self.server.client if names[0] == 'None' else self.server.clients[names[0]]

            result = await DistributedAPI(**json.loads(request, object_hook=as_callable),
                                          logger=self.logger,
                                          node=node).distribute_function()
            task_id = await node.send_string(json.dumps(result).encode())
            if task_id.startswith(b'Error'):
                self.logger.error(task_id)
                result = await node.send_request(b'dapi_err', name_2.encode() + task_id, b'dapi_err')
            else:
                result = await node.send_request(b'dapi_res', name_2.encode() + task_id, b'dapi_err')
            if result.startswith(b'Error'):
                self.logger.error(result)

    def add_request(self, request: bytes):
        """
        Adds request to the queue

        :param request: Request to add
        """
        self.logger.info("Receiving request: {}".format(request))
        self.request_queue.put_nowait(request.decode())


class CallableEncoder(json.JSONEncoder):
    def default(self, obj):

        if callable(obj):
            result = {'__callable__': {}}
            attributes = result['__callable__']
            if hasattr(obj, '__name__'):
                attributes['__name__'] = obj.__name__
            if hasattr(obj, '__module__'):
                attributes['__module__'] = obj.__module__
            if hasattr(obj, '__qualname__'):
                attributes['__qualname__'] = obj.__qualname__
            if hasattr(obj, '__self__'):
                if isinstance(obj.__self__, Wazuh):
                    attributes['__wazuh__'] = obj.__self__.to_dict()
            attributes['__type__'] = type(obj).__name__
            return result

        return json.JSONEncoder.default(self, obj)


def as_callable(dct: Dict):
    try:
        if '__callable__' in dct:
            encoded_callable = dct['__callable__']
            funcname = encoded_callable['__name__']
            if '__wazuh__' in encoded_callable:
                # Encoded Wazuh instance method
                wazuh_dict = encoded_callable['__wazuh__']
                wazuh = Wazuh(ossec_path=wazuh_dict.get('path', '/var/ossec'))
                return getattr(wazuh, funcname)
            else:
                # Encoded function or static method
                qualname = encoded_callable['__qualname__'].split('.')
                classname = qualname[0] if len(qualname) > 1 else None
                module_path = encoded_callable['__module__']
                module = import_module(module_path)
                if classname is None:
                    return getattr(module, funcname)
                else:
                    return getattr(getattr(module, classname), funcname)
        return dct
    except (KeyError, AttributeError):
        raise TypeError(f"Wazuh object cannot be decoded from JSON {encoded_callable}")
