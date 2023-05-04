from collections import defaultdict
from enum import Enum
from typing import Generic, TypeVar, Union, Optional, Protocol
import dataclasses as dc
from dataclasses import dataclass, field
from textwrap import indent

import networkx  # type: ignore

from .util import assert_never


class VarVisibility(Enum):
    PLAINTEXT = "plaintext"
    SHARED = "shared"

    def __str__(self) -> str:
        return self.value

    # __repr__ = __str__


class DataType(Enum):
    INT = "int"
    BOOL = "bool"
    TUPLE = "tuple"

    def __str__(self) -> str:
        return self.value

    # __repr__ = __str__


@dataclass
class VarType:
    visibility: Optional[VarVisibility] = None
    _dims: Optional[int] = None
    datatype: Optional[DataType] = None
    tuple_types: list["VarType"] = field(default_factory=list)

    # For vectorized accesses, we need to know the size of the dimensions
    dim_sizes: Optional[list["LoopBound"]] = None

    @property
    def dims(self) -> Optional[int]:
        # If we have dim_sizes, then we are vectorized.  We treat vectorized
        # values as scalar.
        if self.dim_sizes is not None:
            return 0
        # Otherwise, we are unknown vectorization - just return what we know
        return self._dims

    @property
    def unvectorized_dims(self) -> Optional[int]:
        # If we have dim_sizes, then we are vectorized.
        if self.dim_sizes is not None:
            return len(self.dim_sizes)
        # Otherwise, we are unknown vectorization - just return what we know
        return self._dims

    def __hash__(self) -> int:
        return hash(
            (
                self.visibility,
                self.datatype,
                self._dims,
                tuple(self.tuple_types),
            )
        )

    # Determines if two types are equivalent if vectorized values are considered scalar.
    def is_equivalent_to(self, o: "VarType") -> bool:
        if self.visibility != o.visibility:
            return False

        if self.datatype != o.datatype:
            return False

        if not all(
            st.is_equivalent_to(ot) for st, ot in zip(self.tuple_types, o.tuple_types)
        ):
            return False

        # If at least one of the types is unvectorized
        if self.dim_sizes is None or o.dim_sizes is None:
            # If we don't know the number of dimensions of either type
            if self._dims is None and o._dims is None:
                return True

            # If we only know the dimensions of one type
            elif self._dims is None or o._dims is None:
                return False

            # When we have unvectorized arrays, the _dims is only used to
            # determine the canonical dimensionality of the array.  All
            # non-vectorized arrays are implemented as single-dimension arrays,
            # so that's all we check here.
            assert self.unvectorized_dims is not None
            assert o.unvectorized_dims is not None
            return (self.unvectorized_dims > 0) == (o.unvectorized_dims > 0)

        # If both types are vectorized, compare the number of dimensions they have
        else:
            return self.unvectorized_dims == o.unvectorized_dims

    def drop_dim(self) -> "VarType":
        return dc.replace(
            self,
            _dims=self.unvectorized_dims - 1
            if self.unvectorized_dims is not None
            else None,
            dim_sizes=self.dim_sizes[:-1] if self.dim_sizes is not None else None,
        )

    def add_dim(self) -> "VarType":
        return dc.replace(self, _dims=self._dims + 1 if self._dims is not None else 1)

    def is_plaintext(self) -> bool:
        return self.visibility == VarVisibility.PLAINTEXT

    def is_shared(self) -> bool:
        return self.visibility == VarVisibility.SHARED

    def could_become(self, supertype: "VarType") -> bool:
        return (
            self.visibility in [supertype.visibility, None]
            and self.datatype in [supertype.datatype, None]
            and self._dims in [supertype._dims, None]
            and all(
                t.could_become(subtype)
                for t, subtype in zip(self.tuple_types, supertype.tuple_types)
            )
            and self.dim_sizes in [supertype.dim_sizes, None]
        )

    def is_complete(self) -> bool:
        return (
            self.visibility is not None
            and self.datatype is not None
            and self._dims is not None
            and (
                self.datatype != DataType.TUPLE
                or all(t.is_complete() for t in self.tuple_types)
            )
            # dim_sizes is optional (only necessary for lifted arrays)
        )

    @staticmethod
    def merge(
        *types: "VarType",
        mixed_shared_plaintext_allowed=True,
        mixed_datatypes_allowed=False,
        use_max_dim_size=False,
    ) -> "VarType":
        assert len(types) > 0

        # TODO: switch all type errors to syntax errors with the locations of the offending
        # code in the original source code

        merged_type = VarType()

        # Determine the visibility of the merged type
        elem_visibilities = [t.visibility for t in types]
        if (
            len(set(filter(lambda x: x is not None, elem_visibilities))) > 1
            and not mixed_shared_plaintext_allowed
        ):
            raise TypeError(
                "Cannot merge types with different visibilities:\n{}".format(
                    "\n".join(repr(t) for t in types)
                )
            )
        if any(v == VarVisibility.SHARED for v in elem_visibilities):
            merged_type.visibility = VarVisibility.SHARED
        elif all(v == VarVisibility.PLAINTEXT for v in elem_visibilities):
            merged_type.visibility = VarVisibility.PLAINTEXT

        # Determine the dimensionality of the merged type
        elem_dims = [t.dims for t in types if t.dims is not None]
        if len(set(elem_dims)) > 0 and use_max_dim_size:
            elem_dims = [max(elem_dims)]

        if len(set(elem_dims)) > 1:
            raise TypeError(
                "Cannot merge types with different dimensionality:\n{}".format(
                    "\n".join(repr(t) for t in types)
                )
            )
        if len(elem_dims) > 0:
            merged_type._dims = elem_dims[0]

        # Determine the datatype of the merged type
        elem_datatypes = [t.datatype for t in types if t.datatype is not None]
        if len(set(elem_datatypes)) > 1 and not mixed_datatypes_allowed:
            raise TypeError(
                "Cannot merge types with different datatypes:\n{}".format(
                    "\n".join(repr(t) for t in types)
                )
            )
        if len(elem_datatypes) > 0:
            merged_type.datatype = elem_datatypes[0]

        # Determine the tuple types of the merged type
        if len(set(len(t.tuple_types) for t in types)) > 1:
            raise TypeError(
                "Cannot merge types with different tuple types:\n{}".format(
                    "\n".join(repr(t) for t in types)
                )
            )
        tuple_len = len(types[0].tuple_types)
        merged_type.tuple_types = [VarType() for _ in range(tuple_len)]

        elem_tuple_types = [[t.tuple_types[i] for t in types] for i in range(tuple_len)]
        for i in range(tuple_len):
            if len(set(elem_tuple_types[i])) > 1:
                raise TypeError(
                    "Cannot merge types with different tuple types:\n{}".format(
                        "\n".join(repr(t) for t in types)
                    )
                )
            if len(elem_tuple_types[i]) > 0:
                merged_type.tuple_types[i] = elem_tuple_types[i][0]

        dim_sizes = [tuple(t.dim_sizes) for t in types if t.dim_sizes is not None]
        if len(set(dim_sizes)) > 0 and use_max_dim_size:
            dim_sizes = [max(dim_sizes, key=len)]

        if len(set(dim_sizes)) > 1:
            raise TypeError(
                "Cannot merge types with different dimension sizes:\n{}".format(
                    "\n".join(repr(t) for t in types)
                )
            )
        if len(set(dim_sizes)) > 0:
            merged_type.dim_sizes = list(dim_sizes[0])

        return merged_type

    def __str__(self) -> str:
        if self.datatype == DataType.TUPLE:
            return "tuple[" + ", ".join(str(t) for t in self.tuple_types) + "]"

        str_rep = f"{self.visibility}["
        if self.dim_sizes is not None:
            for _ in self.dim_sizes:
                str_rep += "list["
        elif self._dims is not None:
            for _ in range(self._dims):
                str_rep += "list["

        str_rep += f"{self.datatype}"

        if self.dim_sizes is not None:
            for bound in self.dim_sizes:
                str_rep += f"; ({bound})]"
        elif self._dims is not None:
            for _ in range(self._dims):
                str_rep += "; ?]"

        str_rep += "]"
        if self.dims is None:
            str_rep += "(unknown dims)"
        return str_rep


PLAINTEXT_INT = VarType(VarVisibility.PLAINTEXT, 0, DataType.INT)


@dataclass(frozen=True)
class Var:
    """A variable named `name`"""

    # This is a string for user-provided variables,
    # and an integer for temporary variables the compiler automatically generates
    name: Union[str, int]

    rename_subscript: Optional[int] = None

    def __str__(self) -> str:
        name = self.name if isinstance(self.name, str) else f"!{self.name}"
        subscript = "" if self.rename_subscript is None else f"!{self.rename_subscript}"
        return name + subscript


class TypeEnv(defaultdict[Var, VarType]):
    def __str__(self) -> str:
        return "\n".join(
            [
                f"{var}: {var_type}"
                for var, var_type in sorted(self.items(), key=lambda x: str(x[0]))
            ]
        )


@dataclass
class Parameter:
    """A function parameter of the form `var: var_type`"""

    var: Var
    var_type: VarType
    default_values: list[str] = field(
        default_factory=list
    )  # generated from example function calls in the input file
    party_idx: Optional[
        int
    ] = None  # stores the party index associated with this parameter, if it is shared

    def __str__(self) -> str:
        return f"{self.var}: {self.var_type}"


@dataclass(frozen=True)
class Constant:
    """A constant with value `value`"""

    value: Union[int, bool]
    datatype: DataType

    def __str__(self) -> str:
        return str(self.value)

    def __eq__(self, o) -> bool:
        return (
            isinstance(o, Constant)
            and self.value == o.value
            and self.datatype == o.datatype
        )


LoopBound = Union[Var, Constant]


class BinOpKind(Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    LT = "<"
    GT = ">"
    LT_E = "<="
    GT_E = ">="
    EQ = "=="
    NOT_EQ = "!="
    AND = "and"
    OR = "or"
    BIT_AND = "&"
    BIT_OR = "|"
    BIT_XOR = "^"

    def get_ret_datatype(self) -> Optional[DataType]:
        if self in (BinOpKind.ADD, BinOpKind.SUB, BinOpKind.MUL, BinOpKind.DIV):
            return DataType.INT

        elif self in (
            BinOpKind.LT,
            BinOpKind.GT,
            BinOpKind.LT_E,
            BinOpKind.GT_E,
            BinOpKind.EQ,
            BinOpKind.NOT_EQ,
            BinOpKind.AND,
            BinOpKind.OR,
        ):
            return DataType.BOOL

        elif self in (BinOpKind.BIT_AND, BinOpKind.BIT_XOR, BinOpKind.BIT_OR):
            return None  # return type is the same as the inputs

        else:
            raise ValueError(f"Unhandled binary operation {self}")

    def get_operand_datatypes(self) -> list[DataType]:
        if self in (
            BinOpKind.ADD,
            BinOpKind.SUB,
            BinOpKind.MUL,
            BinOpKind.DIV,
            BinOpKind.LT,
            BinOpKind.GT,
            BinOpKind.LT_E,
            BinOpKind.GT_E,
        ):
            return [DataType.INT]

        elif self in (
            BinOpKind.EQ,
            BinOpKind.NOT_EQ,
            BinOpKind.AND,
            BinOpKind.OR,
            BinOpKind.BIT_AND,
            BinOpKind.BIT_OR,
            BinOpKind.BIT_XOR,
        ):
            return [DataType.INT, DataType.BOOL]

        else:
            raise ValueError(f"Unhandled binary operation {self}")

    def __str__(self) -> str:
        return self.value


class UnaryOpKind(Enum):
    NEGATE = "-"
    NOT = "not"

    def get_ret_datatype(self) -> DataType:
        if self == UnaryOpKind.NEGATE:
            return DataType.INT

        elif self == UnaryOpKind.NOT:
            return DataType.BOOL

        else:
            raise ValueError(f"Unhandled unary operation {self}")

    def get_operand_datatypes(self) -> list[DataType]:
        if self == UnaryOpKind.NEGATE:
            return [DataType.INT]

        elif self == UnaryOpKind.NOT:
            return [DataType.BOOL, DataType.INT]

        else:
            raise ValueError(f"Unhandled unary operation {self}")

    def __str__(self) -> str:
        return self.value


OPERAND = TypeVar("OPERAND")


@dataclass
class BinOp(Generic[OPERAND]):
    """A binary operator expression of the form `left operator right`"""

    left: OPERAND
    operator: BinOpKind
    right: OPERAND

    def __str__(self) -> str:
        return f"({self.left} {self.operator} {self.right})"

    def __hash__(self) -> int:
        return hash((self.left, self.operator, self.right))


@dataclass
class UnaryOp(Generic[OPERAND]):
    """A unary operator expression of the form `operator operand`"""

    operator: UnaryOpKind
    operand: OPERAND

    def __str__(self) -> str:
        return f"{self.operator} {self.operand}"

    def __hash__(self) -> int:
        return hash((self.operator, self.operand))


class SubscriptIndexBinOp(BinOp["SubscriptIndex"]):
    pass


class SubscriptIndexUnaryOp(UnaryOp["SubscriptIndex"]):
    pass


SubscriptIndex = Union[Var, Constant, SubscriptIndexBinOp, SubscriptIndexUnaryOp]


def subscript_index_accessed_vars(index: SubscriptIndex) -> list[Var]:
    if isinstance(index, Var):
        return [index]
    elif isinstance(index, Constant):
        return []
    elif isinstance(index, SubscriptIndexBinOp):
        left = subscript_index_accessed_vars(index.left)
        right = subscript_index_accessed_vars(index.right)
        return left + right
    elif isinstance(index, SubscriptIndexUnaryOp):
        return subscript_index_accessed_vars(index.operand)
    else:
        assert_never(index)


@dataclass(frozen=True)
class Subscript:
    """An array subscript expression of the form `array[index]`"""

    array: Var
    index: SubscriptIndex

    def __hash__(self) -> int:
        return id(self)

    def __str__(self) -> str:
        return f"{self.array}[{self.index}]"


@dataclass
class VectorizedAccess:
    """A vectorized array.  Can only be accessed via row-major ordering."""

    array: Var
    dim_sizes: tuple[LoopBound, ...]
    vectorized_dims: tuple[bool, ...]
    idx_vars: tuple[Var, ...]

    def __str__(self) -> str:
        vectorized_idxs = ", ".join(
            f"{size}".upper()
            for idx_var, size, vectorized in zip(
                self.idx_vars, self.dim_sizes, self.vectorized_dims
            )
            if vectorized
        )
        vectorized_idxs = "{" + vectorized_idxs + "}"
        unvectorized_idxs = ", ".join(
            str(var).lower()
            for var, vectorized in zip(self.idx_vars, self.vectorized_dims)
            if not vectorized
        )
        unvectorized_idxs = "[" + unvectorized_idxs + "]"
        return f"{self.array}{vectorized_idxs}{unvectorized_idxs}"

    def __hash__(self) -> int:
        return hash((self.array, self.dim_sizes, self.vectorized_dims, self.idx_vars))


BLOCK = TypeVar("BLOCK")


@dataclass
class CFGFunction(Generic[BLOCK]):
    name: str
    parameters: list[Parameter]
    body: networkx.DiGraph
    entry_block: BLOCK
    exit_block: BLOCK
    return_type: Optional[VarType]

    def __str__(self) -> str:
        parameters = ", ".join([str(parameter) for parameter in self.parameters])
        block_indices = {block: i for i, block in enumerate(self.body.nodes)}
        blocks = "\n".join(
            [
                f"Block {i}:\n{indent(str(block), '    ')}"
                for i, block in enumerate(self.body.nodes)
            ]
        )
        edges = " ".join(
            [
                f"({block_indices[u]}, {block_indices[v]}, {label})"
                for u, v, label in self.body.edges(data="label")
            ]
        )
        return (
            f"Function {self.name}({parameters})"
            + (f" -> {self.return_type}" if self.return_type is not None else "")
            + ":\n"
            + f"Entry block: {block_indices[self.entry_block]}\n"
            + f"Exit block: {block_indices[self.exit_block]}\n"
            + blocks
            + "\n"
            + f"Edges: {edges}"
        )
