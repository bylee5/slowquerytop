from slowquerytop.collectors.fingerprint import checksum, fingerprint


def test_literals_comments_whitespace():
    sql = "SELECT  *  FROM t1 /* hint */ WHERE id = 42 AND name = 'O''Brien' -- trailing\n AND x = 1.5e3"
    assert fingerprint(sql) == "select * from t1 where id = ? and name = ? and x = ?"


def test_identifiers_with_digits_are_kept():
    assert fingerprint("select col1 from tbl2 where t3.c4 = 5") == "select col1 from tbl2 where t3.c4 = ?"


def test_in_list_and_multi_values_collapse():
    assert fingerprint("SELECT * FROM t WHERE id IN (1, 2, 3)") == "select * from t where id in(?+)"
    assert fingerprint("INSERT INTO t (a,b) VALUES (1,'x'),(2,'y'),(3,'z')") == "insert into t (a,b) values(?+)"


def test_limit_and_use():
    assert fingerprint("select a from t limit 10, 20") == "select a from t limit ?"
    assert fingerprint("use shop") == "use ?"


def test_mysql_hash_comment_and_double_quoted_strings():
    assert fingerprint('select "abc" # comment\nfrom t') == "select ? from t"


def test_mssql_dialect():
    sql = "SELECT TOP (50) [Order Id], \"Total\" FROM #tmp WHERE Name = N'홍길동' AND Code = 0x1F"
    assert fingerprint(sql, "mssql") == 'select top ? [order id], "total" from #tmp where name = ? and code = ?'


def test_same_shape_same_checksum():
    a = fingerprint("select * from t where id = 1")
    b = fingerprint("SELECT *\n  FROM t\n WHERE id = 99999")
    assert a == b
    assert checksum(a) == checksum(b)
    assert len(checksum(a)) == 16
