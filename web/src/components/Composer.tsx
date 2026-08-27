import { useState } from "react";

export function Composer({
  streaming,
  onAsk,
  onStop,
}: {
  streaming: boolean;
  onAsk: (text: string) => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState("");

  const submit = () => {
    const text = value.trim();
    if (!text || streaming) return;
    setValue("");
    onAsk(text);
  };

  return (
    <div className="composer">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <textarea
          rows={2}
          value={value}
          placeholder="Ask a question about the trip data…"
          data-testid="composer-input"
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        {streaming ? (
          <button type="button" onClick={onStop} data-testid="stop">
            Stop
          </button>
        ) : (
          <button type="submit" className="primary" data-testid="send" disabled={!value.trim()}>
            Ask
          </button>
        )}
      </form>
    </div>
  );
}
