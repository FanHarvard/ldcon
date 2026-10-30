"""Absolute device-side candidate/rate constraints with bounded numeric ASTs."""
import ast
import hashlib
import json
import math
import operator
from ldcon.core.contracts import DeviceError


class ConditionError(DeviceError):
    pass


def finite(value, name='value'):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ConditionError(f'{name} must be finite numeric data')
    return float(value)


class Formula:
    """No eval/import/objects: finite scalars, dotted dict fields and min/max/abs."""
    binary={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv}
    compare={ast.Lt:operator.lt,ast.LtE:operator.le,ast.Gt:operator.gt,ast.GtE:operator.ge,ast.Eq:operator.eq,ast.NotEq:operator.ne}
    calls={'abs':abs,'min':min,'max':max}

    def __init__(self,text):
        if not isinstance(text,str) or len(text)>1024:raise ValueError('Expected a formula of at most 1024 characters')
        try:self.tree=ast.parse(text,mode='eval').body
        except (SyntaxError,RecursionError) as e:raise ValueError('Invalid device formula') from e
        if sum(1 for _ in ast.walk(self.tree))>128:raise ValueError('Device formula too complex')
        self.text=text;self._validate(self.tree)

    def _validate(self,n):
        if isinstance(n,ast.Constant):
            if type(n.value) not in (int,float,bool) or not math.isfinite(n.value):raise ValueError('Invalid formula constant')
        elif isinstance(n,ast.Name):
            if n.id.startswith('_'):raise ValueError('Private formula name')
        elif isinstance(n,ast.Attribute):
            if n.attr.startswith('_'):raise ValueError('Private formula field')
            self._validate(n.value)
        elif isinstance(n,ast.BinOp) and type(n.op) in self.binary:self._validate(n.left);self._validate(n.right)
        elif isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub,ast.Not)):self._validate(n.operand)
        elif isinstance(n,ast.Compare) and all(type(op) in self.compare for op in n.ops):
            for child in [n.left,*n.comparators]:self._validate(child)
        elif isinstance(n,ast.BoolOp) and isinstance(n.op,(ast.And,ast.Or)):
            for child in n.values:self._validate(child)
        elif isinstance(n,ast.IfExp):
            for child in (n.test,n.body,n.orelse):self._validate(child)
        elif isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in self.calls and not n.keywords:
            if not 1<=len(n.args)<=8:raise ValueError('Invalid formula argument count')
            for child in n.args:self._validate(child)
        else:raise ValueError('Unsupported device formula operation')

    def value(self,values):
        def checked(value):
            if type(value) is bool:return value
            return finite(value,'formula result')
        def field(n):
            if isinstance(n,ast.Name):return values[n.id]
            item=field(n.value)
            if not isinstance(item,dict):raise ConditionError('Formula fields must select dictionaries')
            return item[n.attr]
        def truth(value):
            if type(value) is not bool:raise ConditionError('Formula condition must be boolean')
            return value
        def visit(n):
            if isinstance(n,ast.Constant):return checked(n.value)
            if isinstance(n,(ast.Name,ast.Attribute)):return checked(field(n))
            if isinstance(n,ast.BinOp):return checked(self.binary[type(n.op)](finite(visit(n.left)),finite(visit(n.right))))
            if isinstance(n,ast.UnaryOp):
                value=visit(n.operand)
                return not truth(value) if isinstance(n.op,ast.Not) else checked(finite(value)*(1 if isinstance(n.op,ast.UAdd) else -1))
            if isinstance(n,ast.Compare):
                args=[visit(child) for child in [n.left,*n.comparators]]
                return all(self.compare[type(op)](a,b) for op,a,b in zip(n.ops,args,args[1:]))
            if isinstance(n,ast.BoolOp):
                args=[truth(visit(child)) for child in n.values]
                return all(args) if isinstance(n.op,ast.And) else any(args)
            if isinstance(n,ast.IfExp):
                # Evaluate both branches: invalid data cannot hide behind a condition.
                condition=truth(visit(n.test));left=visit(n.body);right=visit(n.orelse)
                return left if condition else right
            if isinstance(n,ast.Call):return checked(self.calls[n.func.id](*[finite(visit(a)) for a in n.args]))
            raise ConditionError('Invalid formula')
        try:return visit(self.tree)
        except (KeyError,TypeError,ValueError,ArithmeticError,OverflowError) as e:
            raise ConditionError(f'Cannot evaluate device formula: {self.text}') from e


def policy_digest(policy):
    return hashlib.sha256(json.dumps(policy,sort_keys=True,allow_nan=False).encode()).hexdigest()


class TemperatureConditions:
    """Both the card adapter and the owning driver evaluate the same policy."""
    def __init__(self,policy,settings=None):
        if not isinstance(policy,dict) or set(policy)!={'bounds_c','max_slew_c_per_s','max_step_c','expressions'}:
            raise ValueError('Explicit temperature bounds, rate, step and expressions are required')
        self.settings=settings or {}
        bounds=policy['bounds_c']
        if not isinstance(bounds,list) or len(bounds)!=2:raise ValueError('Temperature bounds are unset')
        self.low,self.high=[finite(v,'temperature bound') for v in bounds]
        if self.low>=self.high:raise ValueError('Temperature bounds must increase')
        value=policy['max_slew_c_per_s']
        self.rate=Formula(value) if isinstance(value,str) else finite(value,'maximum slew')
        if isinstance(self.rate,float) and self.rate<=0:raise ValueError('Maximum slew must be positive')
        self.step=finite(policy['max_step_c'],'maximum step')
        if self.step<=0:raise ValueError('Maximum step must be positive')
        if not isinstance(policy['expressions'],list):raise ValueError('Device conditions must be a list')
        self.expressions=[Formula(text) for text in policy['expressions']]
        self.policy=policy;self.digest=policy_digest(policy)

    def maximum_rate(self,state,candidate,dt_s=0,rate_c_per_s=0):
        value=self.rate.value({'settings':self.settings,'state':state,'candidate':{'temperature_c':candidate},'dt_s':dt_s,'rate_c_per_s':rate_c_per_s}) if isinstance(self.rate,Formula) else self.rate
        value=finite(value,'evaluated maximum slew')
        if value<=0:raise ConditionError('Device maximum slew is not positive')
        return value

    def check(self,state,candidate,elapsed_s,*,previous=None):
        candidate=finite(candidate,'temperature candidate');dt=finite(elapsed_s,'elapsed time')
        if dt<=0:raise ConditionError('Elapsed time must be positive')
        previous=finite(state['setpoint_c'] if previous is None else previous,'previous setpoint')
        if not self.low<=previous<=self.high or not self.low<=candidate<=self.high:
            raise ConditionError('Absolute temperature envelope exceeded')
        measured=finite(state['temperature_c'],'measured temperature')
        if not self.low<=measured<=self.high:raise ConditionError('Measured temperature exceeds absolute envelope')
        delta=abs(candidate-previous);rate=delta/dt
        if delta>self.step+1e-12:raise ConditionError('Absolute temperature step exceeded')
        if rate>self.maximum_rate(state,candidate,dt,rate)+1e-12:raise ConditionError('Absolute temperature slew exceeded')
        values={'settings':self.settings,'state':state,'candidate':{'temperature_c':candidate},'dt_s':dt,'rate_c_per_s':rate}
        for expr in self.expressions:
            if expr.value(values) is not True:raise ConditionError('Absolute device condition is false: '+expr.text)
        return candidate
