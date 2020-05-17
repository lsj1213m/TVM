import tvm
from tvm import relay
from tvm.contrib import graph_runtime
from tvm.relay import testing
import copy
from math import floor, ceil, log2
import numpy as np
from os import path, _exit
import pickle
import threading
import time

class Partitioner:
    def __init__(self, env):
        self.env = env
        self.table_path = 'perf_table'
        self.test_batches_cpu = [1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65]
        self.test_batches_gpu = [1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127, 128, 129]
        self.perf_table = {}
        
        self.benchmark_time = 0.0
        self.offload_trial = 1
        self.tolerate_limit = 5
        self.base_var_limit = 5
        self.max_thresh = 1.05
        
    def CheckPerfTable(self):
        dev_dict = {}
        new_devs = 0

        print('Checking Performance Table...')
        if path.exists(self.table_path):
            with open(self.table_path, 'rb') as table_file:
                self.perf_table = pickle.load(table_file)

        for dev in self.env.devices:
            test_batches = self.test_batches_gpu
            if dev.dev_type == 'cpu':
                test_batches = self.test_batches_cpu

            if dev.name in self.perf_table and \
                self.env.network in self.perf_table[dev.name]:
                    print(dev.name, 'exists in the table')
                    continue
            else:
                if dev.name not in self.perf_table:
                    self.perf_table[dev.name] = {}
                print('testing', dev.name, '...')

            bench_time = time.time()
            dev_dict.clear()

            for batch_size in test_batches:
                if batch_size == 0: continue
                dev.batch_size = batch_size

                # build_time = time.time()
                net, params, input_shape, output_shape = \
                    self.env.get_network(name=self.env.network, batch_size=dev.batch_size)
                with relay.build_config(opt_level=self.env.opt_level):
                    graph, lib, params = relay.build(net, target=dev.target, params=params)
                # build_time = time.time() - build_time
                # print('<%s> build time: %.3f sec' % (dev.name, build_time))

                result = dev.Run(graph, lib, params, input_shape, 
                                self.env.test_times, 'test') / batch_size
                if result <= 0: break
                dev_dict[batch_size] = result

            self.perf_table[dev.name][self.env.network] = copy.deepcopy(dev_dict)
            print('%s (%s)\n%s' % (dev.name, self.env.network, dev_dict))
            new_devs += 1

            bench_time = time.time() - bench_time
            self.benchmark_time += bench_time

        if new_devs > 0:
            bench_time = time.time()
            with open(self.table_path, 'wb') as table_file:
                pickle.dump(self.perf_table, table_file)
            bench_time = time.time() - bench_time
            self.benchmark_time += bench_time

    def FindDev(self, attr):
        best_dev = None
        max_val = float('-inf')
        min_val = float('inf')
        
        for dev in self.env.devices:
            if attr == 'max_diff': # Max Value
                if dev.diff >= max_val:
                    best_dev = dev
                    max_val = dev.diff

            elif attr == 'base_init': # Min Time
                dev_time = self.EstimateDevTime(dev, self.env.batch_size)
                if dev_time < min_val:
                    best_dev = dev
                    min_val = dev_time
            
            elif attr == 'base_next': # Max Time
                dev_time = self.EstimateDevTime(dev, dev.batch_size)
                if dev_time > max_val:
                    best_dev = dev
                    max_val = dev_time

            else:
                print('[Error] Unknown Attribute %s in FindDev' % (attr))
                _exit(1)

        return best_dev

    def EstimateDevTime(self, dev, batch_size):
        if batch_size == 0:
            return 0

        dev_perf = self.perf_table[dev.name][self.env.network]
        test_batches = self.test_batches_gpu
        if dev.dev_type == 'cpu':
            test_batches = self.test_batches_cpu

        if batch_size in dev_perf:
            return dev_perf[batch_size] * batch_size
        
        max_key = max(dev_perf.keys())
        if batch_size > max_key:
            return dev_perf[max_key] * batch_size

        min_val = 2**int(log2(batch_size))+1
        max_val = 2*(min_val-1)-1
        xp = [min_val, max_val]
        yp = [dev_perf[xp[0]], dev_perf[xp[1]]]
        return np.interp(batch_size, xp, yp) * batch_size

    def OffloadDev(self, offload_dev, base_dev, max_time):
        if self.offload_trial > base_dev.batch_size:
            return
        
        dev_times = []
        off_dev_time = 0.0
        for dev in self.env.devices:
            if dev == base_dev: continue

            batch_size = dev.batch_size
            if dev == offload_dev:
                batch_size += self.offload_trial
            eval_time = self.EstimateDevTime(dev, batch_size)
            
            if dev == offload_dev:
                off_dev_time = eval_time
            dev_times.append(eval_time)

        # print(offload_dev.name, dev_times, max_time)

        for dev_time in dev_times:
            if dev_time > max_time * self.max_thresh:
                return

        offload_dev.trial = self.offload_trial
        offload_dev.eval_time = off_dev_time
        offload_dev.diff = max_time - offload_dev.eval_time


    def StartPartition(self):
        self.CheckPerfTable()
        print('\nStart Partitioning...')

        for dev in self.env.devices:
            dev.batch_size = 0

        base_dev = self.FindDev('base_init')
        base_dev.batch_size = self.env.batch_size

        cnt = 0
        offloaded_cnt = 1
        tolerate_cnt = base_var_cnt = 0
        threads = []

        search_time = time.time()
        while base_var_cnt < self.base_var_limit:
            loop_time = time.time()
            
            if cnt > 0 and tolerate_cnt > self.tolerate_limit:
                base_dev = self.FindDev('base_next')
                base_var_cnt += 1
                tolerate_cnt = 0

            max_time = self.EstimateDevTime(base_dev, base_dev.batch_size - 1)

            self.offload_trial = tolerate_cnt + 1
            for dev in self.env.devices:
                dev.trial = 0
                dev.diff = float('-inf')

            if len(self.env.devices) > 2:
                for dev in self.env.devices:
                    if dev == base_dev: continue
                    t = threading.Thread(target=self.OffloadDev, args=(dev, base_dev, max_time))
                    threads.append(t)
                    t.start()
                for t in threads:
                    t.join()
            else: # threads are not needed
                for dev in self.env.devices:
                    if dev == base_dev: continue
                    self.OffloadDev(dev, base_dev, max_time)

            offload_dev = self.FindDev('max_diff')
            if offload_dev is None: break
            offloaded_cnt = offload_dev.trial

            if offloaded_cnt > 0:
                base_dev.batch_size -= offloaded_cnt
                offload_dev.batch_size += offloaded_cnt
                tolerate_cnt = 0
            else:
                tolerate_cnt += 1
            cnt += 1
            loop_time = time.time() - loop_time
            print("[%2d]" % (cnt), self.env.GetBatches(), "%.2f ms" % (loop_time * 1000))
            # print('')

            if base_dev.batch_size == 1:
                break

        for dev in self.env.devices:
            dev.predict_time = self.EstimateDevTime(dev, dev.batch_size)
        search_time = (time.time() - search_time) * 1000
        print('Partitioning finished in %.2f ms\n' % (search_time))
