import os, sys, sysconfig, threading
def f20(): return int(open('/proc/self/stat').read().rsplit(')',1)[1].split()[17])
def names():
    d='/proc/self/task'
    out=[]
    for t in os.listdir(d):
        try: out.append(open(f'{d}/{t}/comm').read().strip())
        except OSError: pass
    return sorted(out)
print(f"build={sys.version.split()[0]} gil_disabled_build={sysconfig.get_config_var('Py_GIL_DISABLED')} "
      f"env_OPENBLAS={os.environ.get('OPENBLAS_NUM_THREADS')}")
print("  at start        : kernel_threads=%d py_threads=%d %s" % (f20(), threading.active_count(), names()))
import numpy
print("  after import np : kernel_threads=%d py_threads=%d %s" % (f20(), threading.active_count(), names()))
a=numpy.ones((600,600)); b=a@a
print("  after matmul    : kernel_threads=%d py_threads=%d %s" % (f20(), threading.active_count(), names()))
try:
    import threadpoolctl
    print("  threadpoolctl   :", [{k:v for k,v in d.items() if k in ('internal_api','num_threads','version')} for d in threadpoolctl.threadpool_info()])
except Exception as e:
    print("  threadpoolctl err", e)
