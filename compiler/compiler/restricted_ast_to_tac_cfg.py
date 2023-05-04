"""
Tools for converting from a restricted subset of Python's AST to
a three-address code control flow graph
"""


import networkx  # type: ignore
from typing import Optional

from . import restricted_ast
from . import tac_cfg
from .util import assert_never


class _CFGBuilder:
    _cfg: networkx.DiGraph
    _entry_block: tac_cfg.Block
    _current_block: tac_cfg.Block
    _tmp_name_counter: int = 0
    _edge_id_counter: int = 0

    def __init__(self):
        self._cfg = networkx.DiGraph()
        self._entry_block = self.make_empty_block()
        self._current_block = self._entry_block

    def generate_variable(self) -> tac_cfg.Var:
        self._tmp_name_counter += 1
        return tac_cfg.Var(self._tmp_name_counter)

    def make_empty_block(self) -> tac_cfg.Block:
        block = tac_cfg.Block(assignments=[], terminator=None, merge_condition=None)
        self._cfg.add_node(block)
        return block

    def get_current_block(self) -> tac_cfg.Block:
        return self._current_block

    def set_current_block(self, block: tac_cfg.Block):
        self._current_block = block
        assert self._current_block.terminator is None

    def add_assignment(self, assignment: tac_cfg.Assign):
        assert self._current_block.terminator is None
        self._current_block.assignments.append(assignment)

    def _add_terminator(self, terminator: tac_cfg.BlockTerminator):
        assert self._current_block.terminator is None
        self._current_block.terminator = terminator

    def _add_branch(self, target_block: tac_cfg.Block, branch_kind: tac_cfg.BranchKind):
        self._edge_id_counter += 1
        self._cfg.add_edge(
            u_of_edge=self._current_block,
            v_of_edge=target_block,
            label=branch_kind,
            ident=self._edge_id_counter,
        )

    def add_jump(self, target_block: tac_cfg.Block):
        self._add_terminator(tac_cfg.Jump())
        self._add_branch(target_block, tac_cfg.BranchKind.UNCONDITIONAL)

    def _add_conditional_jump_edges(self, false_block, true_block):
        self._add_branch(false_block, tac_cfg.BranchKind.FALSE)
        self._add_branch(true_block, tac_cfg.BranchKind.TRUE)

    def add_conditional_jump(
        self,
        condition: tac_cfg.Var,
        false_block: tac_cfg.Block,
        true_block: tac_cfg.Block,
    ):
        self._add_terminator(tac_cfg.ConditionalJump(condition))
        self._add_conditional_jump_edges(false_block, true_block)

    def add_for(
        self,
        counter: tac_cfg.Var,
        bound_low: tac_cfg.LoopBound,
        bound_high: tac_cfg.LoopBound,
        body_block: tac_cfg.Block,
        after_block: tac_cfg.Block,
    ):
        self._add_terminator(tac_cfg.For(counter, bound_low, bound_high))
        self._add_conditional_jump_edges(after_block, body_block)

    def build_function(
        self,
        name: str,
        parameters: list[tac_cfg.Parameter],
        return_var: tac_cfg.Var,
        return_type: Optional[tac_cfg.VarType],
    ) -> tac_cfg.Function:
        assert self._current_block.terminator is None
        self._current_block.terminator = tac_cfg.Return(value=return_var)
        return tac_cfg.Function(
            name=name,
            parameters=parameters,
            body=self._cfg,
            entry_block=self._entry_block,
            exit_block=self._current_block,
            return_type=return_type,
        )


def _build_expression_as_var(
    expression: restricted_ast.Expression, builder: _CFGBuilder
) -> tac_cfg.Var:
    atom = _build_expression_as_atom(expression, builder)
    if isinstance(atom, tac_cfg.Var):
        return atom
    elif isinstance(atom, tac_cfg.Constant):
        var = builder.generate_variable()
        builder.add_assignment(tac_cfg.Assign(lhs=var, rhs=atom))
        return var
    else:
        assert not isinstance(atom, tac_cfg.VectorizedAccess)
        assert_never(atom)


def _build_expression_as_atom(
    expression: restricted_ast.Expression, builder: _CFGBuilder
) -> tac_cfg.Atom:
    assign_rhs = _build_expression_as_rhs(expression, builder)
    if isinstance(assign_rhs, (tac_cfg.Var, tac_cfg.Constant)):
        return assign_rhs
    else:
        var = builder.generate_variable()
        builder.add_assignment(tac_cfg.Assign(lhs=var, rhs=assign_rhs))
        return var


def _build_expression_as_operand(
    expression: restricted_ast.Expression, builder: _CFGBuilder
) -> tac_cfg.Operand:
    assign_rhs = _build_expression_as_rhs(expression, builder)
    if isinstance(assign_rhs, tac_cfg.Subscript):
        return assign_rhs
    else:
        return _build_expression_as_atom(expression, builder)


def _build_expression_as_rhs(
    expression: restricted_ast.Expression, builder: _CFGBuilder
) -> tac_cfg.AssignRHS:
    if (
        isinstance(expression, restricted_ast.Var)
        or isinstance(expression, restricted_ast.Constant)
        or isinstance(expression, restricted_ast.Subscript)
    ):
        return expression
    elif isinstance(expression, restricted_ast.List):
        return tac_cfg.List(
            items=[
                _build_expression_as_atom(item, builder) for item in expression.items
            ]
        )
    elif isinstance(expression, restricted_ast.Tuple):
        return tac_cfg.Tuple(
            items=[
                _build_expression_as_atom(item, builder) for item in expression.items
            ]
        )
    elif isinstance(expression, restricted_ast.BinOp):
        return tac_cfg.BinOp(
            left=_build_expression_as_operand(expression.left, builder),
            operator=expression.operator,
            right=_build_expression_as_operand(expression.right, builder),
        )
    elif isinstance(expression, restricted_ast.UnaryOp):
        return tac_cfg.UnaryOp(
            operator=expression.operator,
            operand=_build_expression_as_operand(expression.operand, builder),
        )
    else:
        assert_never(expression)


def _build_assignment(assignment: restricted_ast.Assign, builder: _CFGBuilder):
    if isinstance(assignment.lhs, restricted_ast.Var):
        lhs = assignment.lhs
        rhs = _build_expression_as_rhs(assignment.rhs, builder)
    elif isinstance(assignment.lhs, restricted_ast.Subscript):
        lhs = assignment.lhs.array
        rhs = tac_cfg.Update(
            array=assignment.lhs.array,
            index=assignment.lhs.index,
            value=_build_expression_as_atom(assignment.rhs, builder),
        )
    else:
        assert_never(assignment.lhs)
    builder.add_assignment(tac_cfg.Assign(lhs=lhs, rhs=rhs))


def _build_if(if_statement: restricted_ast.If, builder: _CFGBuilder):
    condition_block = builder.get_current_block()
    else_block = builder.make_empty_block()
    then_block = builder.make_empty_block()
    after_block = builder.make_empty_block()

    builder.add_conditional_jump(
        condition=_build_expression_as_var(if_statement.condition, builder),
        false_block=else_block,
        true_block=then_block,
    )

    builder.set_current_block(else_block)
    _build_statements(if_statement.else_body, builder)
    builder.add_jump(after_block)

    builder.set_current_block(then_block)
    _build_statements(if_statement.then_body, builder)
    builder.add_jump(after_block)

    builder.set_current_block(after_block)
    assert isinstance(condition_block.terminator, tac_cfg.ConditionalJump)
    after_block.merge_condition = condition_block.terminator


def _build_for(for_loop: restricted_ast.For, builder: _CFGBuilder):
    # Jump to a new block so the end of the loop can jump back here
    condition_block = builder.make_empty_block()
    builder.add_jump(condition_block)
    builder.set_current_block(condition_block)

    # Block for loop body
    body_block = builder.make_empty_block()

    # Block for code after the loop
    after_block = builder.make_empty_block()

    # Build the loop condition
    builder.add_for(
        counter=for_loop.counter,
        bound_low=for_loop.bound_low,
        bound_high=for_loop.bound_high,
        body_block=body_block,
        after_block=after_block,
    )

    # Add the loop body
    builder.set_current_block(body_block)
    _build_statements(for_loop.body, builder)

    # Jump back to condition
    builder.add_jump(condition_block)

    # Continue writing code after the loop
    builder.set_current_block(after_block)


def _build_statements(statements: list[restricted_ast.Statement], builder: _CFGBuilder):
    for statement in statements:
        if isinstance(statement, restricted_ast.For):
            _build_for(statement, builder)
        elif isinstance(statement, restricted_ast.If):
            _build_if(statement, builder)
        elif isinstance(statement, restricted_ast.Assign):
            _build_assignment(statement, builder)
        else:
            assert_never(statement)


def restricted_ast_to_tac_cfg(node: restricted_ast.Function) -> tac_cfg.Function:
    builder = _CFGBuilder()
    _build_statements(node.body, builder)
    return_var = _build_expression_as_var(node.return_value, builder)
    return builder.build_function(
        node.name, node.parameters, return_var, node.return_type
    )
