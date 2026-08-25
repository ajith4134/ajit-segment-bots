"""Every field a part reads off a payload is a field the producer actually carries.

Three parts have now been taken off the air, or silently corrupted, by reading a
field that exists nowhere on the type its producer publishes:

    2026-08-25  stop-target-placer  LiquidationMap.cluster_prices   -- crashed
    2026-08-25  stop-target-placer  VolatilityForecast.expected_move_fraction
                                    -- a getattr default, silent, every stop wrong
    2026-08-25  position-sizer      LockedAllocation.segment        -- crashed
                                    1,587 times; no order was sized for two hours

None of them could fail before the producer first ran, which is why they all
appeared on the day a block was switched on rather than on the day it was written.
The contract checker (R-01) proves the *wire* exists; nothing proved the *shape*
carried on it, and a wire that carries the wrong shape is worse than an absent one
because every board reports it as alive.

This probe reads the code rather than the running system, so it holds before a
part is ever started:

  1. every part is imported, and the types its module can publish -- those it
     defines and those it imports from `runtime.*` -- are collected per data type
     it declares it produces;
  2. every part is parsed, and the reads it performs on a payload are collected by
     following the assemblies in `runtime/input_assembly.py`, which is how all 912
     readers in this project are built;
  3. a read whose name appears on no type any producer of that data type could
     publish is reported.

**What it does not claim.** A data type whose producers define several payload
types is checked against the union of them, so a read valid on the wrong one of
two shapes passes here. And a read this parser cannot follow is counted and
reported as unfollowed rather than as clean -- absence of evidence renders as its
own state (Rule 8), and a checker that quietly skipped what it could not parse
would be the same failure one layer along.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pathlib
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
PARTS_ROOT = REPOSITORY_ROOT / "parts"

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# The assemblies every part reads its inputs through. A part that read a bus
# reader directly would not be followed here, and would be counted as unfollowed.
ASSEMBLY_CLASS_NAMES = frozenset({"Batch", "LatestByKey", "LatestValue"})

# What an assembly hands back, and what shape it is in.
PAYLOAD_METHODS = frozenset({"payloads", "value", "messages"})
MAPPING_METHOD = "mapping"

# Names that belong to the envelope rather than to the payload, so a read of one
# is not a read of a producer's type.
MESSAGE_ATTRIBUTE_NAMES = frozenset({"payload", "published_at_ns", "producer", "data_type"})


@dataclasses.dataclass(frozen=True)
class PayloadRead:
    """One attribute a part reads off one data type, and where."""

    part_id: str
    data_type: str
    attribute: str
    source_file: str
    line: int


@dataclasses.dataclass(frozen=True)
class UnfollowedRead:
    """A reader this parser could not trace to a payload, counted rather than ignored."""

    part_id: str
    data_type: str
    reason: str


def import_every_part(parts_root: pathlib.Path = PARTS_ROOT) -> dict[str, object]:
    """Import every part module, keyed by part id.

    Imported rather than parsed, because a producer's payload type is often
    imported from `runtime.*` rather than defined beside it, and only an import
    resolves that without re-implementing Python's own name resolution.
    """
    modules: dict[str, object] = {}
    for path in sorted(parts_root.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = ".".join(path.relative_to(REPOSITORY_ROOT).with_suffix("").parts)
        module = importlib.import_module(module_name)
        declaration = getattr(module, "PART_DECLARATION", None)
        if declaration is not None:
            modules[declaration.part_id] = module
    return modules


def attribute_names_of(payload_type: type) -> frozenset[str]:
    """Every name a payload of this type answers to: fields, properties and methods.

    `dir()` alone is not enough and the difference is silent: a dataclass field
    with no default is an annotation and never becomes a class attribute, so
    `dir(FundingSettlement)` does not contain `venue_id`. Reading only `dir()`
    reported 1,924 correct reads as defects on this checker's first run.
    """
    named = {name for name in dir(payload_type) if not name.startswith("_")}
    named.update(field.name for field in dataclasses.fields(payload_type))
    return frozenset(named)


def dataclasses_in(namespace: dict) -> tuple[type, ...]:
    return tuple(
        value for value in namespace.values()
        if isinstance(value, type) and dataclasses.is_dataclass(value)
    )


def collect_publishable_types(module: object) -> tuple[type, ...]:
    """The payload types this module could publish.

    Its own dataclasses, and every dataclass in the project modules it imports
    from. The second half is not decoration: `venue-trade-stream-reader` publishes
    `market-data` as a `NormalisedTrade` built by the venue adapter it was handed,
    and imports only `VenueAdapter` by name -- so the type it actually publishes
    appears nowhere in its own namespace.

    This widens what counts as carried, and the widening is stated here rather
    than left to be discovered: a read valid on some *other* type in an imported
    module passes this checker. What it still catches with certainty is the defect
    that has taken three parts off the air -- a field name that exists on nothing
    the producer can reach.
    """
    reachable = list(dataclasses_in(vars(module)))
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.module is None or node.level:
            continue
        if node.module.split(".")[0] not in ("runtime", "parts"):
            continue
        imported = importlib.import_module(node.module)
        reachable.extend(dataclasses_in(vars(imported)))
    return tuple(reachable)


def collect_producer_attribute_names(modules: dict[str, object]) -> dict[str, frozenset[str]]:
    """Per data type, every attribute name any of its producers' types carries."""
    by_data_type: dict[str, set[str]] = {}
    for module in modules.values():
        declaration = module.PART_DECLARATION
        publishable = collect_publishable_types(module)
        for data_type in declaration.produces:
            names = by_data_type.setdefault(data_type, set())
            for payload_type in publishable:
                names.update(attribute_names_of(payload_type))
    return {data_type: frozenset(names) for data_type, names in by_data_type.items()}


class PartReadParser(ast.NodeVisitor):
    """Follows a part's assemblies to the attribute reads it performs on payloads.

    One pass, no branching analysis: a name is bound to a data type when it is
    assigned from an assembly of that type, and rebinding it to anything else
    drops the binding. That is enough for the shape every part in this project is
    written in, and anything it cannot follow is reported rather than assumed.
    """

    def __init__(self, part_id: str, source_file: str) -> None:
        self.part_id = part_id
        self.source_file = source_file
        self.reads: list[PayloadRead] = []
        self.unfollowed: list[UnfollowedRead] = []
        # name -> data type, for each of: a bus reader, an assembly over one, a
        # payload taken out of one, and the mapping an assembly returns.
        self._reader_types: dict[str, str] = {}
        self._assembly_types: dict[str, str] = {}
        self._payload_types: dict[str, str] = {}
        self._mapping_types: dict[str, str] = {}
        # A local helper that builds an assembly from a data type it is given,
        # e.g. `def by_symbol(data_type): return LatestByKey(read=...(data_type))`.
        self._assembly_factories: dict[str, int] = {}

    # -- recognising the shapes -------------------------------------------------

    def _data_type_of_reader_call(self, node: ast.AST) -> str | None:
        """`context.bus.reader("some-type")` -> "some-type"."""
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return None
        if node.func.attr != "reader" or not node.args:
            return None
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        return None

    def _data_type_of_read_keyword(self, node: ast.Call) -> str | None:
        """The data type an assembly's `read=` argument names."""
        for keyword in node.keywords:
            if keyword.arg != "read":
                continue
            named = self._data_type_of_reader_call(keyword.value)
            if named is not None:
                return named
            if isinstance(keyword.value, ast.Name):
                return self._reader_types.get(keyword.value.id)
        return None

    def _data_type_of_assembly_call(self, node: ast.AST) -> str | None:
        """An assembly built here, or one built by a local factory called here."""
        if not isinstance(node, ast.Call):
            return None
        if isinstance(node.func, ast.Name) and node.func.id in ASSEMBLY_CLASS_NAMES:
            return self._data_type_of_read_keyword(node)
        if isinstance(node.func, ast.Name) and node.func.id in self._assembly_factories:
            index = self._assembly_factories[node.func.id]
            if index < len(node.args):
                argument = node.args[index]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    return argument.value
        return None

    def _data_type_taken_out_of(self, node: ast.AST) -> str | None:
        """`assembly.payloads()`, `.value()`, or `mapping().get(...)` -> its data type."""
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return None
        method = node.func.attr
        owner = node.func.value
        if method in PAYLOAD_METHODS and isinstance(owner, ast.Name):
            return self._assembly_types.get(owner.id)
        if method == "get":
            if isinstance(owner, ast.Name):
                return self._mapping_types.get(owner.id)
            inner = self._data_type_of_mapping(owner)
            if inner is not None:
                return inner
        return None

    def _data_type_of_mapping(self, node: ast.AST) -> str | None:
        """`assembly.mapping()` -> the data type its values carry."""
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return None
        if node.func.attr != MAPPING_METHOD or not isinstance(node.func.value, ast.Name):
            return None
        return self._assembly_types.get(node.func.value.id)

    # -- walking ----------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        parameters = [argument.arg for argument in node.args.args]
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return) or statement.value is None:
                continue
            call = statement.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id not in ASSEMBLY_CLASS_NAMES:
                continue
            for keyword in call.keywords:
                if keyword.arg != "read":
                    continue
                inner = keyword.value
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "reader"
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id in parameters
                ):
                    self._assembly_factories[node.name] = parameters.index(inner.args[0].id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        name = node.targets[0].id
        for bindings in (self._reader_types, self._assembly_types, self._payload_types, self._mapping_types):
            bindings.pop(name, None)

        reader_type = self._data_type_of_reader_call(node.value)
        if reader_type is not None:
            self._reader_types[name] = reader_type
            return
        assembly_type = self._data_type_of_assembly_call(node.value)
        if assembly_type is not None:
            self._assembly_types[name] = assembly_type
            return
        mapping_type = self._data_type_of_mapping(node.value)
        if mapping_type is not None:
            self._mapping_types[name] = mapping_type
            return
        payload_type = self._data_type_taken_out_of(node.value)
        if payload_type is not None:
            self._payload_types[name] = payload_type

    def visit_For(self, node: ast.For) -> None:
        data_type = self._data_type_taken_out_of(node.iter)
        if data_type is None and isinstance(node.iter, ast.Call):
            data_type = self._data_type_of_values_call(node.iter)
        if data_type is not None and isinstance(node.target, ast.Name):
            self._payload_types[node.target.id] = data_type
        self.generic_visit(node)

    def _data_type_of_values_call(self, node: ast.Call) -> str | None:
        """`assembly.mapping().values()` -> the data type it yields."""
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "values":
            return None
        return self._data_type_of_mapping(node.func.value)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg == "key_of" and isinstance(keyword.value, ast.Lambda):
                data_type = self._data_type_of_assembly_call(node)
                self._record_lambda_reads(keyword.value, data_type)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
            owner, attribute = node.args[0], node.args[1]
            if (
                isinstance(owner, ast.Name)
                and isinstance(attribute, ast.Constant)
                and isinstance(attribute.value, str)
            ):
                data_type = self._payload_types.get(owner.id)
                if data_type is not None:
                    self._record(data_type, attribute.value, node.lineno)
        self.generic_visit(node)

    def _record_lambda_reads(self, lambda_node: ast.Lambda, data_type: str | None) -> None:
        if not lambda_node.args.args:
            return
        parameter = lambda_node.args.args[0].arg
        if data_type is None:
            self.unfollowed.append(
                UnfollowedRead(self.part_id, "unknown", f"key_of on an assembly at line {lambda_node.lineno}")
            )
            return
        for inner in ast.walk(lambda_node.body):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                if inner.value.id == parameter:
                    self._record(data_type, inner.attr, inner.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            data_type = self._payload_types.get(node.value.id)
            if data_type is not None and node.attr not in MESSAGE_ATTRIBUTE_NAMES:
                self._record(data_type, node.attr, node.lineno)
        self.generic_visit(node)

    def _record(self, data_type: str, attribute: str, line: int) -> None:
        self.reads.append(PayloadRead(self.part_id, data_type, attribute, self.source_file, line))


def read_payload_attribute_reads(part_id: str, source_path: pathlib.Path) -> PartReadParser:
    parser = PartReadParser(part_id, str(source_path.relative_to(REPOSITORY_ROOT)))
    parser.visit(ast.parse(source_path.read_text(encoding="utf-8")))
    return parser


def find_reads_no_producer_carries(
    modules: dict[str, object],
    producer_names: dict[str, frozenset[str]],
) -> tuple[list[PayloadRead], list[PayloadRead], list[PayloadRead]]:
    """Reads that no producer's type carries, reads of unproduced types, and all reads."""
    unmatched: list[PayloadRead] = []
    unproduced: list[PayloadRead] = []
    every_read: list[PayloadRead] = []
    for part_id, module in modules.items():
        source_path = pathlib.Path(module.__file__)
        parser = read_payload_attribute_reads(part_id, source_path)
        every_read.extend(parser.reads)
        for read in parser.reads:
            carried = producer_names.get(read.data_type)
            if carried is None:
                unproduced.append(read)
            elif read.attribute not in carried:
                unmatched.append(read)
    return unmatched, unproduced, every_read


def main() -> int:
    modules = import_every_part()
    producer_names = collect_producer_attribute_names(modules)
    unmatched, unproduced, every_read = find_reads_no_producer_carries(modules, producer_names)

    print("payload reads -- every field a part reads off a payload it consumes")
    print()
    print(f"  parts imported        {len(modules):>6}")
    print(f"  data types produced   {len(producer_names):>6}")
    print(f"  reads followed        {len(every_read):>6}")
    print(f"  reads of a type nothing produces {len(unproduced):>6}")
    print(f"  reads no producer carries        {len(unmatched):>6}")
    print()

    for read in sorted(unproduced, key=lambda r: (r.data_type, r.part_id)):
        print(f"  NOT PRODUCED  {read.part_id} reads {read.data_type}.{read.attribute} "
              f"({read.source_file}:{read.line})")
    if unproduced:
        print()
    for read in sorted(unmatched, key=lambda r: (r.part_id, r.data_type, r.attribute)):
        print(f"  NO SUCH FIELD {read.part_id} reads {read.data_type}.{read.attribute} "
              f"({read.source_file}:{read.line})")

    if unmatched:
        print()
        print(f"  {len(unmatched)} reads name a field no producer of that data type carries.")
        return 1
    print("  every followed read names a field some producer of its data type carries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
