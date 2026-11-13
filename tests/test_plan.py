from slowquerytop import demo, plan


def _demo_xml():
    return demo._DEMO_PLAN.format(text="select 1", cost="10.0", cost_scan="9.0", cost_seek="0.5")


def test_summarize_operators_and_missing_index():
    s = plan.summarize(_demo_xml())
    assert not s.error
    assert s.statement_cost == 10.0 and s.optimization_level == "FULL"
    assert [n.physical_op for n in s.nodes] == ["Nested Loops", "Clustered Index Scan", "Clustered Index Seek"]
    assert [n.depth for n in s.nodes] == [0, 1, 1]
    root, scan, seek = s.nodes
    assert root.obj == ""  # the join itself references no object
    assert scan.obj == "[shop].[dbo].[Orders].[PK_Orders]"
    assert "CustomerId" in scan.extra
    assert seek.obj.endswith("[PK_Customers]")
    assert round(root.cost_pct, 1) == 5.0 and round(scan.cost_pct, 1) == 90.0
    (mi,) = s.missing_indexes
    assert mi["impact"] == 87.4
    assert mi["ddl"] == "CREATE INDEX [IX_suggested] ON [shop].[dbo].[Orders] ([CustomerId]) INCLUDE ([Total])"


def test_invalid_xml_is_reported_not_raised():
    s = plan.summarize("<not xml")
    assert s.error.startswith("XML parse error")
