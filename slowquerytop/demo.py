"""Synthetic data so the UI can be explored without a SQL Server."""

from __future__ import annotations

import math
import random
from datetime import timedelta

from sqlalchemy import Engine

from .collectors.base import CollectResult, HistoryRow, PlanRow
from .collectors.fingerprint import checksum as fp_checksum
from .collectors.fingerprint import fingerprint
from .repository import store

_QUERIES = [
    ("SELECT o.OrderId, o.Total FROM dbo.Orders o WHERE o.CustomerId = @p1 AND o.Status = N'OPEN'", 0.004, 120),
    ("SELECT TOP (50) * FROM dbo.Products WHERE Name LIKE N'%' + @p1 + N'%' ORDER BY Price DESC", 0.35, 9000),
    ("UPDATE dbo.Inventory SET Qty = Qty - @p1 WHERE ProductId = @p2", 0.002, 6),
    ("SELECT c.*, a.* FROM dbo.Customers c JOIN dbo.Addresses a ON a.CustomerId = c.CustomerId WHERE c.Email = @p1", 0.012, 40),
    ("INSERT INTO dbo.AuditLog (UserId, Action, CreatedAt) VALUES (@p1, @p2, SYSUTCDATETIME())", 0.001, 3),
    ("SELECT COUNT(*) FROM dbo.Events WHERE CreatedAt >= DATEADD(day, -30, SYSUTCDATETIME())", 2.4, 180000),
    ("EXEC dbo.usp_MonthlyReport @Year = 2026, @Month = 10", 11.0, 950000),
    ("SELECT u.UserId, u.LastLogin FROM dbo.Users u WITH (NOLOCK) WHERE u.LastLogin < @p1", 0.8, 42000),
    ("DELETE FROM dbo.Sessions WHERE ExpiresAt < SYSUTCDATETIME()", 0.6, 25000),
    ("SELECT p.ProductId, SUM(oi.Qty) FROM dbo.OrderItems oi JOIN dbo.Products p ON p.ProductId = oi.ProductId GROUP BY p.ProductId", 4.2, 400000),
    ("SELECT * FROM dbo.Coupons WHERE Code = @p1", 0.003, 4),
    ("MERGE dbo.DailyStats AS t USING (SELECT @p1 AS Day) s ON t.Day = s.Day WHEN MATCHED THEN UPDATE SET Hits = Hits + 1", 0.02, 30),
]

_DEMO_PLAN = """<?xml version="1.0" encoding="utf-16"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan" Version="1.564" Build="16.0.1000.6">
 <BatchSequence><Batch><Statements>
  <StmtSimple StatementText="{text}" StatementId="1" StatementCompId="1" StatementType="SELECT"
              StatementSubTreeCost="{cost}" StatementOptmLevel="FULL" CardinalityEstimationModelVersion="160">
   <QueryPlan DegreeOfParallelism="1" CachedPlanSize="32">
    <MissingIndexes>
     <MissingIndexGroup Impact="87.4">
      <MissingIndex Database="[shop]" Schema="[dbo]" Table="[Orders]">
       <ColumnGroup Usage="EQUALITY"><Column Name="[CustomerId]" ColumnId="2"/></ColumnGroup>
       <ColumnGroup Usage="INCLUDE"><Column Name="[Total]" ColumnId="5"/></ColumnGroup>
      </MissingIndex>
     </MissingIndexGroup>
    </MissingIndexes>
    <MemoryGrantInfo SerialRequiredMemory="0" SerialDesiredMemory="0" GrantedMemory="0"/>
    <RelOp NodeId="0" PhysicalOp="Nested Loops" LogicalOp="Inner Join" EstimateRows="12" EstimatedTotalSubtreeCost="{cost}">
     <NestedLoops Optimized="0">
      <RelOp NodeId="1" PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan" EstimateRows="12" EstimatedTotalSubtreeCost="{cost_scan}">
       <IndexScan Ordered="0">
        <Object Database="[shop]" Schema="[dbo]" Table="[Orders]" Index="[PK_Orders]"/>
        <Predicate><ScalarOperator ScalarString="[shop].[dbo].[Orders].[CustomerId]=[@p1]"/></Predicate>
       </IndexScan>
      </RelOp>
      <RelOp NodeId="2" PhysicalOp="Clustered Index Seek" LogicalOp="Clustered Index Seek" EstimateRows="1" EstimatedTotalSubtreeCost="{cost_seek}">
       <IndexScan Ordered="1">
        <Object Database="[shop]" Schema="[dbo]" Table="[Customers]" Index="[PK_Customers]"/>
       </IndexScan>
      </RelOp>
     </NestedLoops>
    </RelOp>
   </QueryPlan>
  </StmtSimple>
 </Statements></Batch></BatchSequence>
</ShowPlanXML>"""


def generate(engine: Engine, days: int = 7, seed: int = 42) -> int:
    rnd = random.Random(seed)
    now = store.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    hosts = [("SQLPROD01", "shop"), ("SQLPROD01", "crm"), ("SQLPROD02", "shop")]
    result = CollectResult(source_type="querystore")

    for qi, (text, base_dur, base_reads) in enumerate(_QUERIES):
        fp = fingerprint(text, "mssql")
        checksum = fp_checksum(fp)
        plan_a = f"{(qi * 7919 + 17) & 0xFFFFFFFF:08X}AAAA0000"
        plan_b = f"{(qi * 7919 + 17) & 0xFFFFFFFF:08X}BBBB0000"
        regress_at = start + timedelta(days=days * 0.6) if qi in (1, 7) else None
        for host, db in hosts:
            if (qi + len(host + db)) % 4 == 0:
                continue
            t = start
            while t < now:
                hour = t.hour
                load = 0.3 + 0.7 * max(0.0, math.sin((hour - 6) / 24 * 2 * math.pi))
                cnt = int(rnd.expovariate(1.0) * (2000 if base_dur < 0.05 else 40) * load) + 1
                plan = plan_a
                factor = 1.0
                if regress_at and t >= regress_at:
                    plan, factor = plan_b, 4.0
                dur = base_dur * factor * rnd.uniform(0.6, 1.6)
                reads = base_reads * factor * rnd.uniform(0.7, 1.3)
                result.rows.append(
                    HistoryRow(
                        hostname=host,
                        db=db,
                        checksum=checksum,
                        fingerprint=fp,
                        sample=text,
                        ts_min=t,
                        ts_max=t + timedelta(hours=1),
                        exec_count=cnt,
                        plan_hash=plan,
                        metrics={
                            "duration_sum": dur * cnt,
                            "duration_min": dur * 0.2,
                            "duration_max": dur * rnd.uniform(2, 8),
                            "cpu_time_sum": dur * cnt * 0.7,
                            "cpu_time_min": dur * 0.1,
                            "cpu_time_max": dur * 3,
                            "lock_time_sum": dur * cnt * rnd.uniform(0, 0.05),
                            "logical_reads_sum": reads * cnt,
                            "logical_reads_max": reads * 3,
                            "physical_reads_sum": reads * cnt * 0.01,
                            "physical_reads_max": reads * 0.1,
                            "writes_sum": (reads * 0.1 * cnt) if text.startswith(("UPDATE", "INSERT", "DELETE", "MERGE")) else 0,
                            "writes_max": 10.0,
                            "rows_returned_sum": rnd.randint(1, 50) * cnt,
                            "rows_returned_max": 500.0,
                            "memory_grant_kb_max": 1024.0 if base_reads > 10000 else 0.0,
                        },
                    )
                )
                t += timedelta(hours=1)
            for plan in {plan_a, plan_b} if regress_at else {plan_a}:
                cost = base_reads / 1000.0 * (4 if plan == plan_b else 1)
                result.plans.append(
                    PlanRow(
                        hostname=host,
                        db=db,
                        checksum=checksum,
                        plan_hash=plan,
                        plan_xml=_DEMO_PLAN.format(
                            text=text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;"),
                            cost=f"{cost:.4f}",
                            cost_scan=f"{cost * 0.9:.4f}",
                            cost_seek=f"{cost * 0.05:.4f}",
                        ),
                        captured_at=now,
                    )
                )
    return store.save_result(engine, result)
