export function Clarification({
  question,
  options,
  onChoose,
  disabled,
}: {
  question: string;
  options: { id: string; label: string }[];
  onChoose: (id: string) => void;
  disabled: boolean;
}) {
  return (
    <div className="clarify">
      <p>{question}</p>
      <div className="options">
        {options.map((o) => (
          <button key={o.id} type="button" onClick={() => onChoose(o.id)} disabled={disabled}>
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}
