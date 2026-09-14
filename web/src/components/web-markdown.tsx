"use client";

/**
 * 极简 Markdown 渲染：标题、表格行、列表、引用、段落。
 *
 * 决策包与答辩状草稿共用同一套渲染，避免两处显示不一致；这里只做展示，
 * 不解析 HTML，也不执行文档里的任何内容（文书正文属于不可信数据）。
 */
export function WebMarkdown({ markdown }: { markdown: string }) {
  const lines = markdown.split("\n");
  return (
    <>
      {lines.map((line, index) => {
        const text = line.trimEnd();
        if (!text.trim()) return <div key={index} style={{ height: 8 }} />;
        const key = `md-${index}`;
        if (text.startsWith("# ")) return <h2 key={key}>{text.slice(2)}</h2>;
        if (text.startsWith("## ")) return <h3 key={key}>{text.slice(3)}</h3>;
        if (text.startsWith("### ")) return <h4 key={key}>{text.slice(4)}</h4>;
        if (text.startsWith("> ")) {
          return (
            <blockquote key={key} style={{ margin: "4px 0", opacity: 0.85 }}>
              {text.slice(2)}
            </blockquote>
          );
        }
        if (text.startsWith("| ")) {
          if (/^\|[\s:|-]+\|$/.test(text)) return null;
          const cells = text.split("|").slice(1, -1).map((cell) => cell.trim());
          return (
            <div
              key={key}
              style={{
                display: "grid",
                gridTemplateColumns: `repeat(${cells.length}, minmax(0, 1fr))`,
                gap: 8,
                fontSize: 13,
                padding: "2px 0",
              }}
            >
              {cells.map((cell, cellIndex) => (
                <span key={`${key}-${cellIndex}`}>{cell}</span>
              ))}
            </div>
          );
        }
        if (text.startsWith("- [ ] ")) return <p key={key}>☐ {text.slice(6)}</p>;
        if (text.startsWith("- ")) return <p key={key}>• {text.slice(2)}</p>;
        return <p key={key}>{text}</p>;
      })}
    </>
  );
}
