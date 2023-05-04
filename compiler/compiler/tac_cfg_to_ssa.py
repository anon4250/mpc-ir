"""
Tools for converting from a three-address code control flow graph
to static single assignment form
"""

from collections import Counter
import itertools
from typing import Optional, Union, cast

import networkx

from compiler.ast_shared import VectorizedAccess  # type: ignore

from . import tac_cfg
from . import ssa
from .util import assert_never


def _compute_blocks_setting_vars(
    function: ssa.Function, include_loop_counters: bool
) -> dict[ssa.Var, set[ssa.Block]]:

    result: dict[ssa.Var, set[ssa.Block]] = dict()

    block: ssa.Block
    for block in function.body.nodes:
        term = block.terminator
        lhss = [a.lhs for a in block.assignments] + (
            [term.counter]
            if include_loop_counters and isinstance(term, ssa.For)
            else []
        )
        for var in lhss:
            assert isinstance(
                var, ssa.Var
            ), "VectorizedAccesses are not added until basic vectorization"
            if var not in result:
                result[var] = set()

            result[var].add(block)

    return result


def _tac_cfg_to_ssa_struct(tac_cfg_function: tac_cfg.Function) -> ssa.Function:
    """
    Convert a `tac_cfg.Function` to an `ssa.Function` without
    trying to enforce any SSA properties.
    The output has no Phi-functions and may assign
    to the same variable multiple times.
    """

    cfg = networkx.DiGraph()
    mapping: dict[tac_cfg.Block, ssa.Block] = dict()

    tac_cfg_block: tac_cfg.Block
    for tac_cfg_block in tac_cfg_function.body.nodes:
        ssa_block = ssa.Block(
            phi_functions=[],
            assignments=tac_cfg_block.assignments,
            terminator=tac_cfg_block.terminator,
            merge_condition=tac_cfg_block.merge_condition,
        )
        cfg.add_node(ssa_block)
        mapping[tac_cfg_block] = ssa_block

    source_block: tac_cfg.Block
    dest_block: tac_cfg.Block
    label: ssa.BranchKind
    for source_block, dest_block, edge_data in tac_cfg_function.body.edges(data=True):
        cfg.add_edge(
            u_of_edge=mapping[source_block],
            v_of_edge=mapping[dest_block],
            label=edge_data["label"],
            ident=edge_data["ident"],
        )

    return ssa.Function(
        name=tac_cfg_function.name,
        parameters=tac_cfg_function.parameters,
        body=cfg,
        entry_block=mapping[tac_cfg_function.entry_block],
        exit_block=mapping[tac_cfg_function.exit_block],
        return_type=tac_cfg_function.return_type,
    )


def place_phi_functions(result: ssa.Function) -> None:
    dominance_frontiers: dict[
        ssa.Block, set[ssa.Block]
    ] = networkx.algorithms.dominance_frontiers(G=result.body, start=result.entry_block)

    blocks_setting_vars = _compute_blocks_setting_vars(
        function=result, include_loop_counters=False
    )

    iter_count = 0
    has_already: Counter[ssa.Block] = Counter()
    work: Counter[ssa.Block] = Counter()
    W: set[ssa.Block] = set()

    for V in blocks_setting_vars.keys():
        iter_count += 1

        for X in blocks_setting_vars[V]:
            work[X] = iter_count
            W.add(X)

        while W != set():
            X = W.pop()
            Y: ssa.Block
            for Y in dominance_frontiers[X]:
                if has_already[Y] < iter_count:
                    num_predecessors = sum(1 for _ in result.body.predecessors(Y))
                    assert num_predecessors == 2
                    Y.phi_functions.append(ssa.Phi(lhs=V, rhs_false=V, rhs_true=V))
                    has_already[Y] = iter_count
                    if work[Y] < iter_count:
                        work[Y] = iter_count
                        W.add(Y)


def rename_variables(result: ssa.Function) -> None:
    dominance_tree = result.compute_dominance_tree()
    blocks_setting_vars = _compute_blocks_setting_vars(
        function=result, include_loop_counters=True
    )

    S: dict[ssa.Var, list[int]] = dict()
    C: dict[ssa.Var, int] = dict()

    param_vars = [param.var for param in result.parameters]

    def rename_var(V: ssa.Var, i: Optional[int] = None):
        if i is None:
            try:
                i = S[V][-1]
            except KeyError:
                return V

        return ssa.Var(V.name, i)

    def rename_subscript_index(index: ssa.SubscriptIndex) -> ssa.SubscriptIndex:
        if isinstance(index, ssa.Var):
            return rename_var(index)
        elif isinstance(index, ssa.Constant):
            return index
        elif isinstance(index, ssa.SubscriptIndexBinOp):
            return ssa.SubscriptIndexBinOp(
                left=rename_subscript_index(index.left),
                operator=index.operator,
                right=rename_subscript_index(index.right),
            )
        elif isinstance(index, ssa.SubscriptIndexUnaryOp):
            return ssa.SubscriptIndexUnaryOp(
                operator=index.operator,
                operand=rename_subscript_index(index.operand),
            )
        else:
            assert_never(index)

    def rename_subscript(subscript: ssa.Subscript) -> ssa.Subscript:
        return ssa.Subscript(
            array=rename_var(subscript.array),
            index=rename_subscript_index(subscript.index),
        )

    def rename_atom(atom: ssa.Atom) -> ssa.Atom:
        if isinstance(atom, ssa.Var):
            return rename_var(atom)
        elif isinstance(atom, ssa.Constant):
            return atom
        else:
            assert not isinstance(atom, ssa.VectorizedAccess)
            assert_never(atom)

    def rename_operand(operand: ssa.Operand) -> ssa.Operand:
        if isinstance(operand, ssa.Var):
            return rename_var(operand)
        elif isinstance(operand, ssa.Constant):
            return operand
        elif isinstance(operand, ssa.Subscript):
            return rename_subscript(operand)
        elif isinstance(operand, ssa.BinOp):
            return cast(ssa.BinOp, rename_rhs(operand))
        elif isinstance(operand, ssa.UnaryOp):
            return cast(ssa.UnaryOp, rename_rhs(operand))
        else:
            assert not isinstance(
                operand, VectorizedAccess
            ), "VectorizedArr is introduced in vectorization phase"
            assert_never(operand)

    def rename_rhs(rhs: ssa.AssignRHS) -> ssa.AssignRHS:
        if isinstance(rhs, ssa.Var):
            return rename_var(rhs)
        elif isinstance(rhs, ssa.Constant):
            return rhs
        elif isinstance(rhs, ssa.Subscript):
            return rename_subscript(rhs)
        elif isinstance(rhs, ssa.BinOp):
            return ssa.BinOp(
                left=rename_operand(rhs.left),
                operator=rhs.operator,
                right=rename_operand(rhs.right),
            )
        elif isinstance(rhs, ssa.UnaryOp):
            return ssa.UnaryOp(
                operator=rhs.operator, operand=rename_operand(rhs.operand)
            )
        elif isinstance(rhs, ssa.List):
            return ssa.List(items=[rename_atom(item) for item in rhs.items])
        elif isinstance(rhs, ssa.Tuple):
            return ssa.Tuple(items=[rename_atom(item) for item in rhs.items])
        elif isinstance(rhs, ssa.Mux):
            return ssa.Mux(
                condition=rename_var(rhs.condition),
                false_value=rename_operand(rhs.false_value),
                true_value=rename_operand(rhs.true_value),
            )
        elif isinstance(rhs, ssa.Update):
            return ssa.Update(
                array=rename_var(rhs.array),
                index=rename_subscript_index(rhs.index),
                value=rename_atom(rhs.value),
            )
        else:
            assert not isinstance(
                rhs,
                (
                    tac_cfg.LiftExpr,
                    tac_cfg.DropDim,
                    VectorizedAccess,
                    tac_cfg.VectorizedUpdate,
                ),
            ), "These types are introduced in the vectorization phase"
            assert_never(rhs)

    def search(X: ssa.Block):
        old_lhs: dict[Union[ssa.Phi, ssa.Assign, ssa.For], ssa.Var] = dict()

        if (
            isinstance(X.terminator, ssa.ConditionalJump)
            or isinstance(X.terminator, ssa.For)
            or isinstance(X.terminator, ssa.Return)
        ):
            var_terminators = [X.terminator]
        elif isinstance(X.terminator, ssa.Jump):
            var_terminators = []
        else:
            assert X.terminator is not None
            assert_never(X.terminator)

        # Required for type checker
        empty1: list[
            Union[ssa.Phi, ssa.Assign, ssa.ConditionalJump, ssa.For, ssa.Return]
        ] = []
        for A in (
            empty1
            + [a for a in X.phi_functions]
            + [a for a in X.assignments]
            + [a for a in var_terminators]
        ):
            if isinstance(A, ssa.Assign):
                A.rhs = rename_rhs(A.rhs)
            elif isinstance(A, ssa.ConditionalJump):
                A.condition = rename_var(A.condition)
            elif isinstance(A, ssa.Return):
                A.value = rename_var(A.value)
            elif isinstance(A, ssa.For):
                if isinstance(A.bound_low, ssa.Var):
                    A.bound_low = rename_var(A.bound_low)
                if isinstance(A.bound_high, ssa.Var):
                    A.bound_high = rename_var(A.bound_high)
            elif isinstance(A, ssa.Phi):
                pass
            else:
                assert_never(A)

            if isinstance(A, (ssa.Phi, ssa.Assign, ssa.For)):
                assert isinstance(
                    A.lhs, ssa.Var
                ), "VectorizedAccesses are not added until basic vectorization"
                V = A.lhs
                i = C[V]
                old_lhs[A] = A.lhs
                A.lhs = rename_var(V, i)
                S[V].append(i)
                C[V] = i + 1

        Y: ssa.Block
        for Y in result.body.successors(X):
            assert len(list(result.body.predecessors(Y))) in (1, 2)
            j = [
                i
                for i, predecessor in enumerate(
                    sorted(
                        result.body.predecessors(Y),
                        key=lambda X_: result.body.edges[X_, Y]["ident"],
                    )
                )
                if predecessor == X
            ][0]
            for F in Y.phi_functions:
                phi_branch_true = {0: False, 1: True}[j]
                assert isinstance(F.rhs_false, ssa.Var) and isinstance(
                    F.rhs_true, ssa.Var
                ), "VectorizedAccesses are not added until basic vectorization"
                V = F.rhs_true if phi_branch_true else F.rhs_false
                i = S[V][-1]
                if isinstance(V, ssa.Subscript):
                    pass  # TODO: Support this
                elif isinstance(V, ssa.Var):
                    if phi_branch_true:
                        F.rhs_true = rename_var(V, i)
                    else:
                        F.rhs_false = rename_var(V, i)
                else:
                    assert_never(V)

        for Y in dominance_tree[X]:
            search(Y)

        empty2: list[Union[ssa.Phi, ssa.Assign]] = []  # Required for type checker
        for A in empty2 + [a for a in X.phi_functions] + [a for a in X.assignments]:
            V = old_lhs[A]
            S[V].pop()

    # In Cytron's SSA paper,
    # all variables V with rename subscript zero (V₀) are assumed to be initialized
    # at the beginning of the program.
    #
    # From Cytron's SSA paper:
    # > Suppose that a variable V₀ has just one assignment in the original program,
    # > so that any use of V will be either a use of the value V. at entry to the
    # > program or a use of the value V₁ from the most recent execution of the
    # > assignment to V.
    #
    # All explicit assignments start at rename subscript one (V₁ = …),
    # and accesses V before the first assignment are given rename subscript zero (V₀).
    # Since parameters have explicit initial values (the values passed into the function),
    # parameters are renamed to have a zero subscript.

    # Assume all variables V have an initial value V₀.
    for V in itertools.chain(param_vars, blocks_setting_vars.keys()):
        C[V] = 1
        S[V] = [0]

    # Rename all parameters V to V₀.
    for param in result.parameters:
        param.var = rename_var(param.var, 0)

    search(result.entry_block)


def tac_cfg_to_ssa(tac_cfg_function: tac_cfg.Function) -> ssa.Function:
    result = _tac_cfg_to_ssa_struct(tac_cfg_function)
    place_phi_functions(result)
    rename_variables(result)
    return result
