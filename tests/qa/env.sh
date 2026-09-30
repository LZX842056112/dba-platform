#!/usr/bin/env bash
# QA 独立验证环境（私有 MySQL 3307）
export DBA_TEST_MYSQL_DSN="mysql+asyncmy://root@127.0.0.1:3307/dba"
export DBA_MYSQL_DSN="mysql+asyncmy://root@127.0.0.1:3307/dba"
export DBA_MYSQL_RO_DSN="mysql+asyncmy://root@127.0.0.1:3307/dba"
