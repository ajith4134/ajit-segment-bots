from runtime.part_declaration import PartDeclaration, ResourceClass

PART_DECLARATION = PartDeclaration(
    part_id='historical-bar-store',
    consumes=('market-data',),
    produces=('historical-window', 'part-health'),
    resource_class=ResourceClass.BANDWIDTH_BOUND,
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)
