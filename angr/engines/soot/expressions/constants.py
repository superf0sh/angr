
from archinfo.arch_soot import SootClassDescriptor, SootNullConstant

from ..values import SimSootValue_StringRef
from .base import SimSootExpr
from ..values import SimSootValue_ThisRef
from ..values import SimSootValue_InstanceFieldRef
from ..values.constants import SimSootValue_ClassConstant


class SimSootExpr_IntConstant(SimSootExpr):
    def _execute(self):
        self.expr = self.state.se.BVV(self.expr.value, 32)

class SimSootExpr_LongConstant(SimSootExpr):
    def __init__(self, expr, state):
        super(SimSootExpr_LongConstant, self).__init__(expr, state)

    def _execute(self):
        self.expr = self.state.se.BVV(self.expr.value, 64)

class SimSootExpr_StringConstant(SimSootExpr):
    def __init__(self, expr, state):
        super(SimSootExpr_StringConstant, self).__init__(expr, state)

    def _execute(self):
        # We need to strip away the quotes introduced by soot in case of a string constant
        self.expr = self.state.se.StringV(self.expr.value.strip("\""))

class SimSootExpr_LongConstant(SimSootExpr):
    def _execute(self):
        self.expr = self.state.se.BVV(self.expr.value, 64)


class SimSootExpr_StringConstant(SimSootExpr):
    def _execute(self):
        # strip away quotes introduced by soot
        str_val = self.state.se.StringV(self.expr.value.strip("\""))
        str_ref = SimSootValue_StringRef(self.state.memory.get_new_uuid())
        self.state.memory.store(str_ref, str_val)
        self.expr = str_ref


class SimSootExpr_ClassConstant(SimSootExpr):
    def _execute(self):
        class_name = self.expr.value[8:-2].replace("/", ".")
        self.expr = SootClassDescriptor(class_name)


class SimSootExpr_NullConstant(SimSootExpr):
    def _execute(self):
        self.expr = SootNullConstant()
