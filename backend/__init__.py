# Use PyMySQL as the MySQLdb driver -- pure Python, so it needs no system
# libraries on the Railway builder. Harmless for local SQLite dev.
try:
    import pymysql

    # Django 6 requires MySQLdb.version_info >= (2, 2, 1); PyMySQL reports lower,
    # so present a compatible version before registering it as MySQLdb.
    pymysql.version_info = (2, 2, 1, "final", 0)
    pymysql.__version__ = "2.2.1"
    pymysql.install_as_MySQLdb()
except ImportError:
    pass
