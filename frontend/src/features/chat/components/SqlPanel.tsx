// SQL 折叠区：展示生成/修正后的 SQL（默认折叠）。

interface Props {
  sql: string | null;
}

export function SqlPanel({ sql }: Props) {
  if (!sql) return null;
  return (
    <details style={{ marginTop: 8 }}>
      <summary className="dim">查看 SQL</summary>
      <pre className="sql-collapse" style={{ marginTop: 6 }}>
        {sql}
      </pre>
    </details>
  );
}
