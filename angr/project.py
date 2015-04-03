#!/usr/bin/env python

# pylint: disable=W0703

import os
import md5
import types
import struct
import logging
import weakref

import cle
import simuvex

l = logging.getLogger("angr.project")

projects = weakref.WeakValueDictionary()
def fake_project_unpickler(name):
    if name not in projects:
        raise AngrError("Project %s has not been opened." % name)
    return projects[name]
fake_project_unpickler.__safe_for_unpickling__ = True

def deprecated(f):
    def deprecated_wrapper(*args, **kwargs):
        print "ERROR: FUNCTION %s IS DEPRECATED. PLEASE UPDATE YOUR CODE." % f
        return f(*args, **kwargs)
    return deprecated_wrapper

class Project(object):
    """
    This is the main class of the Angr module. It is meant to contain a set of
    binaries and the relationships between them, and perform analyses on them.
    """

    def __init__(self, filename,
                 use_sim_procedures=True,
                 default_analysis_mode=None,
                 exclude_sim_procedure=None,
                 exclude_sim_procedures=(),
                 arch=None,
                 osconf=None,
                 load_options=None,
                 parallel=False, ignore_functions=None,
                 argv=None, envp=None, symbolic_argc=None):
        """
        This constructs a Project object.

        Arguments:
            @filename: path to the main executable object to analyse
            @arch: optional target architecture (auto-detected otherwise)
            in the form of a simuvex.SimState or a string
            @exclude_sim_procedures: a list of functions to *not* wrap with
            simprocedures
            @exclude_sim_procedure: a function that, when passed a function
            name, returns whether or not to wrap it with a simprocedure

            @load_options: a dict of {binary1: {option1:val1, option2:val2 etc.}}
            e.g., {'/bin/ls':{backend:'ida', skip_libs='ld.so.2', auto_load_libs=False}}

            See CLE's documentation for valid options.
        """

        if isinstance(exclude_sim_procedure, types.LambdaType):
            l.warning("Passing a lambda type as the exclude_sim_procedure argument to Project causes the resulting object to be un-serializable.")

        if not os.path.exists(filename) or not os.path.isfile(filename):
            raise Exception("Not a valid binary file: %s" % repr(filename))

        if not default_analysis_mode:
            default_analysis_mode = 'symbolic'

        self.irsb_cache = {}
        self.binaries = {}
        self.dirname = os.path.dirname(filename)
        self.basename = os.path.basename(filename)
        self.filename = filename
        projects[filename] = self

        self.default_analysis_mode = default_analysis_mode if default_analysis_mode is not None else 'symbolic'
        self._exclude_sim_procedure = exclude_sim_procedure
        self._exclude_sim_procedures = exclude_sim_procedures
        self.exclude_all_sim_procedures = exclude_sim_procedures
        self._parallel = parallel
        self.load_options = { } if load_options is None else load_options

        # List of functions we don't want to step into (and want
        # ReturnUnconstrained() instead)
        self.ignore_functions = [] if ignore_functions is None else ignore_functions

        self._cfg = None
        self._vfg = None
        self._cdg = None
        self._analysis_results = { }
        self.results = AnalysisResults(self)

        self.analyses = Analyses(self, self._analysis_results)
        self.surveyors = Surveyors(self)

        # This is a map from IAT addr to (SimProcedure class name, kwargs_)
        self.sim_procedures = {}

        l.info("Loading binary %s", self.filename)
        l.debug("... from directory: %s", self.dirname)

        # ld is angr's loader, provided by cle
        self.ld = cle.Ld(filename, self.load_options)
        self.main_binary = self.ld.main_bin

        if arch in simuvex.Architectures:
            self.arch = simuvex.Architectures[arch](self.ld.main_bin.get_vex_ir_endness())
        elif isinstance(arch, simuvex.SimArch):
            self.arch = arch
        elif arch is None:
            self.arch = simuvex.Architectures[self.ld.main_bin.simarch](self.ld.main_bin.get_vex_ir_endness())
        else:
            raise ValueError("Invalid arch specification.")

        self.min_addr = self.ld.min_addr()
        self.max_addr = self.ld.max_addr()
        self.entry = self.ld.main_bin.entry

        if use_sim_procedures == True:
            self.use_sim_procedures()

            # We need to resync memory as simprocedures have been set at the
            # level of each IDA's instance
            if self.ld.ida_main == True:
                self.ld.ida_sync_mem()

        # command line arguments, environment variables, etc
        self.argv = argv
        self.envp = envp
        self.symbolic_argc = symbolic_argc

        if isinstance(osconf, OSConf) and osconf.arch == self.arch:
            self.osconf = osconf #pylint:disable=invalid-name
        elif osconf is None:
            self.osconf = LinuxConf(self.arch, self)
        else:
            raise ValueError("Invalid OS specification or non-matching architecture.")

        self.osconf.configure_project(self)

        self.vexer = VEXer(self.ld.memory, self.arch, use_cache=self.arch.cache_irsb)
        self.capper = Capper(self.ld.memory, self.arch, use_cache=True)
        self.state_generator = StateGenerator(self)
        self.path_generator = PathGenerator(self)

    #
    # Pickling
    #

    def __getstate__(self):
        try:
            vexer, capper, ld, main_bin, state_generator = self.vexer, self.capper, self.ld, self.main_binary, self.state_generator
            self.vexer, self.capper, self.ld, self.main_binary, self.state_generator = None, None, None, None, None
            return dict(self.__dict__)
        finally:
            self.vexer, self.capper, self.ld, self.main_binary, self.state_generator = vexer, capper, ld, main_bin, state_generator

    def __setstate__(self, s):
        self.__dict__.update(s)
        self.ld = cle.Ld(self.filename, self.load_options)
        self.main_binary = self.ld.main_bin
        self.vexer = VEXer(self.ld.memory, self.arch, use_cache=self.arch.cache_irsb)
        self.capper = Capper(self.ld.memory, self.arch, use_cache=True)
        self.state_generator = StateGenerator(self)

    #
    # Project stuff
    #

    def exclude_sim_procedure(self, f):
        return (f in self._exclude_sim_procedures) or (self._exclude_sim_procedure is not None and self._exclude_sim_procedure(f))

    def __find_sim_libraries(self):
        """ Look for libaries that we can replace with their simuvex
        simprocedures counterpart
        This function returns the list of libraries that were found in simuvex
        """
        simlibs = []

        auto_libs = [os.path.basename(o) for o in self.ld.dependencies.keys()]
        custom_libs = [os.path.basename(o) for o in self.ld._custom_dependencies.keys()]

        libs = set(auto_libs + custom_libs + self.ld._get_static_deps(self.main_binary))

        for lib_name in libs:
            # Hack that should go somewhere else:
            if lib_name == 'libc.so.0':
                lib_name = 'libc.so.6'

            if lib_name == 'ld-uClibc.so.0':
                lib_name = 'ld-uClibc.so.6'

            if lib_name not in simuvex.procedures.SimProcedures:
                l.debug("There are no simprocedures for library %s :(", lib_name)
            else:
                simlibs.append(lib_name)

        return simlibs

    def use_sim_procedures(self):
        """ Use simprocedures where we can """

        libs = self.__find_sim_libraries()
        unresolved = []

        for obj in [self.main_binary] + self.ld.shared_objects:
            functions = obj.imports

            for i in functions:
                unresolved.append(i)

            l.debug("[Resolved [R] SimProcedures]")
            for i in functions:
                if self.exclude_sim_procedure(i):
                    # l.debug("%s: SimProcedure EXCLUDED", i)
                    continue

                for lib in libs:
                    simfun = simuvex.procedures.SimProcedures[lib]
                    if i not in simfun.keys():
                        continue
                    l.debug("[R] %s:", i)
                    l.debug("\t -> matching SimProcedure in %s :)", lib)
                    self.set_sim_procedure(obj, lib, i, simfun[i], None)
                    unresolved.remove(i)

            # What's left in imp is unresolved.
            if len(unresolved) > 0:
                l.debug("[Unresolved [U] SimProcedures]: using ReturnUnconstrained instead")

            for i in unresolved:
                # Where we cannot use SimProcedures, we step into the function's
                # code (if you don't want this behavior, use 'auto_load_libs':False
                # in load_options)
                if self.exclude_sim_procedure(i):
                    continue

                if i in obj.resolved_imports \
                        and i not in self.ignore_functions \
                        and i in obj.jmprel \
                        and not (obj.jmprel[i] >= obj.get_min_addr()
                                 and obj.jmprel[i] <= obj.get_max_addr()):
                    continue
                l.debug("[U] %s", i)
                self.set_sim_procedure(obj, "stubs", i,
                                       simuvex.SimProcedures["stubs"]["ReturnUnconstrained"],
                                       {'resolves':i})

    def update_jmpslot_with_simprocedure(self, func_name, pseudo_addr, binary):
        """ Update a jump slot (GOT address referred to by a PLT slot) with the
        address of a simprocedure """
        self.ld.override_got_entry(func_name, pseudo_addr, binary)

    def add_custom_sim_procedure(self, address, sim_proc, kwargs):
        '''
        Link a SimProcedure class to a specified address.
        '''
        if address in self.sim_procedures:
            l.warning("Address 0x%08x is already in SimProcedure dict.", address)
            return
        if kwargs is None: kwargs = {}
        self.sim_procedures[address] = (sim_proc, kwargs)

    def is_sim_procedure(self, hashed_addr):
        return hashed_addr in self.sim_procedures

    def get_pseudo_addr_for_sim_procedure(self, s_proc):
        for addr, tpl in self.sim_procedures.items():
            simproc_class, _ = tpl
            if isinstance(s_proc, simproc_class):
                return addr
        return None

    def set_sim_procedure(self, binary, lib, func_name, sim_proc, kwargs):
        """
         Generate a hashed address for this function, which is used for
         indexing the abstract function later.
         This is so hackish, but thanks to the fucking constraints, we have no
         better way to handle this
        """
        m = md5.md5()
        m.update(lib + "_" + func_name)

        # TODO: update addr length according to different system arch
        hashed_bytes = m.digest()[:self.arch.bits/8]
        pseudo_addr = (struct.unpack(self.arch.struct_fmt, hashed_bytes)[0] / 4) * 4

        # Put it in our dict
        if kwargs is None: kwargs = {}
        if (pseudo_addr in self.sim_procedures) and \
                            (self.sim_procedures[pseudo_addr][0] != sim_proc):
            l.warning("Address 0x%08x is already in SimProcedure dict.", pseudo_addr)
            return

        # Special case for __libc_start_main - it needs to call exit() at the end of execution
        # TODO: Is there any more elegant way of doing this?
        if func_name == '__libc_start_main':
            if 'exit_addr' not in kwargs:
                m = md5.md5()
                m.update('__libc_start_main:exit')
                hashed_bytes_ = m.digest()[ : self.arch.bits / 8]
                pseudo_addr_ = (struct.unpack(self.arch.struct_fmt, hashed_bytes_)[0] / 4) * 4
                self.sim_procedures[pseudo_addr_] = (simuvex.procedures.SimProcedures['libc.so.6']['exit'], {})
                kwargs['exit_addr'] = pseudo_addr_

        self.sim_procedures[pseudo_addr] = (sim_proc, kwargs)
        l.debug("\t -> setting SimProcedure with pseudo_addr 0x%x...", pseudo_addr)

        # TODO: move this away from Project
        # Is @binary using the IDA backend ?
        if isinstance(binary, cle.IdaBin):
            binary.resolve_import_with(func_name, pseudo_addr)
            #binary.resolve_import_dirty(func_name, pseudo_addr)
        else:
            self.update_jmpslot_with_simprocedure(func_name, pseudo_addr, binary)

    def initial_exit(self, mode=None, options=None):
        """Creates a SimExit to the entry point."""
        return self.exit_to(addr=self.entry, mode=mode, options=options)

    @deprecated
    def initial_state(self, mode=None, add_options=None, args=None, env=None, **kwargs):
        '''
        Creates an initial state, with stack and everything.

        All arguments are passed directly through to StateGenerator.entry_point,
        allowing for a couple of more reasonable defaults.

        @param mode - Optional, defaults to project.default_analysis_mode
        @param add_options - gets PARALLEL_SOLVES added to it if project._parallel is true
        @param args - Optional, defaults to project.argv
        @param env - Optional, defaults to project.envp
        '''

        # Have some reasonable defaults
        if mode is None:
            mode = self.default_analysis_mode
        if add_options is None:
            add_options = set()
        if self._parallel:
            add_options |= { simuvex.o.PARALLEL_SOLVES }
        if args is None:
            args = self.argv
        if env is None:
            env = self.envp

        return self.state_generator.entry_point(mode=mode, add_options=add_options, args=args, env=env, **kwargs)

    @deprecated
    def exit_to(self, addr=None, state=None, mode=None, options=None, initial_prefix=None):
        '''
        Creates a Path with the given state as initial state.

        :param addr:
        :param state:
        :param mode:
        :param options:
        :param jumpkind:
        :param initial_prefix:
        :return: A Path instance
        '''
        return self.path_generator.blank_path(address=addr, mode=mode, options=options,
                        initial_prefix=initial_prefix, state=state)

    def block(self, addr, max_size=None, num_inst=None, traceflags=0, thumb=False, backup_state=None):
        """
        Returns a pyvex block starting at address addr

        Optional params:

        @param max_size: the maximum size of the block, in bytes
        @param num_inst: the maximum number of instructions
        @param traceflags: traceflags to be passed to VEX. Default: 0
        @thumb: bool: this block is in thumb mode (ARM)
        """
        return self.vexer.block(addr, max_size=max_size, num_inst=num_inst,
                                traceflags=traceflags, thumb=thumb, backup_state=backup_state)

    def sim_block(self, state, max_size=None, num_inst=None,
                  stmt_whitelist=None, last_stmt=None, addr=None):
        """
        Returns a simuvex block starting at SimExit @where

        Optional params:

        @param where: the exit to start the analysis at
        @param max_size: the maximum size of the block, in bytes
        @param num_inst: the maximum number of instructions
        @param state: the initial state. Fully unconstrained if None

        """
        if addr is None:
            addr = state.se.any_int(state.regs.ip)

        thumb = False
        if addr % state.arch.instruction_alignment != 0:
            if state.thumb:
                thumb = True
            else:
                import ipdb; ipdb.set_trace()
                raise AngrExitError("Address 0x%x does not align to alignment %d "
                                    "for architecture %s." % (addr,
                                    state.arch.instruction_alignment,
                                    state.arch.name))

        irsb = self.block(addr, max_size, num_inst, thumb=thumb, backup_state=state)
        return simuvex.SimIRSB(state, irsb, addr=addr, whitelist=stmt_whitelist, last_stmt=last_stmt)

    def sim_run(self, state, max_size=400, num_inst=None, stmt_whitelist=None,
                last_stmt=None, jumpkind="Ijk_Boring"):
        """
        Returns a simuvex SimRun object (supporting refs() and
        exits()), automatically choosing whether to create a SimIRSB or
        a SimProcedure.

        Parameters:
        @param state : the state to analyze
        @param max_size : the maximum size of the block, in bytes
        @param num_inst : the maximum number of instructions
        @param state : the initial state. Fully unconstrained if None
        """

        addr = state.se.any_int(state.regs.ip)

        if jumpkind == "Ijk_Sys_syscall":
            l.debug("Invoking system call handler (originally at 0x%x)", addr)
            return simuvex.SimProcedures['syscalls']['handler'](state, addr=addr)

        if jumpkind in ("Ijk_EmFail", "Ijk_NoDecode", "Ijk_MapFail") or "Ijk_Sig" in jumpkind:
            l.debug("Invoking system call handler (originally at 0x%x)", addr)
            r = simuvex.SimProcedures['syscalls']['handler'](state, addr=addr)
        elif self.is_sim_procedure(addr):
            sim_proc_class, kwargs = self.sim_procedures[addr]
            l.debug("Creating SimProcedure %s (originally at 0x%x)",
                    sim_proc_class.__name__, addr)
            state._inspect('call', simuvex.BP_BEFORE, function_name=sim_proc_class.__name__)
            r = sim_proc_class(state, addr=addr, sim_kwargs=kwargs)
            state._inspect('call', simuvex.BP_AFTER, function_name=sim_proc_class.__name__)
            l.debug("... %s created", r)
        else:
            l.debug("Creating SimIRSB at 0x%x", addr)
            r = self.sim_block(state, max_size=max_size, num_inst=num_inst,
                                  stmt_whitelist=stmt_whitelist,
                                  last_stmt=last_stmt, addr=addr)

        return r

    def binary_by_addr(self, addr):
        """ This returns the binary containing address @addr"""
        return self.ld.addr_belongs_to_object(addr)

    #
    # Non-deprecated analyses
    #

    def analyzed(self, name, *args, **kwargs):
        key = (name, args, tuple(sorted(kwargs.items())))
        return key in self._analysis_results

from .errors import AngrMemoryError, AngrExitError, AngrError
from .vexer import VEXer
from .capper import Capper
from .analysis import AnalysisResults, Analyses
from .surveyor import Surveyors
from .states import StateGenerator
from .paths import PathGenerator
from .osconf import OSConf, LinuxConf
