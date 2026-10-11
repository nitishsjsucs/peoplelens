import type { AskResponse } from "./api";
import { formatCell, hiddenCount } from "./format";

export function ResultTable({ answer, grain }: { answer: NonNullable<AskResponse["answer"]>; grain?: string }) {
  const hidden = hiddenCount(answer.suppressed_cells);
  return (
    <div className="result">
      <p className="narrative">{answer.narrative}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {answer.columns.map((c) => (
                <th key={c.name} className={c.kind === "measure" ? "num" : ""}>
                  {c.name.replaceAll("_", " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {answer.rows.map((row, i) => (
              <tr key={i} className={row["suppressed"] === true ? "suppressed" : ""}>
                {answer.columns.map((c) => (
                  <td key={c.name} className={c.kind === "measure" ? "num" : ""}>
                    {formatCell(c, row, grain)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hidden > 0 && (
        <p className="note">
          {hidden} {hidden === 1 ? "value is" : "values are"} hidden: groups of fewer than 10 people are never shown, and neither is
          anything that would let them be worked out.
        </p>
      )}
    </div>
  );
}
