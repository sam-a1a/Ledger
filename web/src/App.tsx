import "./styles/tokens.css";
import "./App.css";

import { Composer } from "./components/Composer";
import { RoleSwitcher } from "./components/RoleSwitcher";
import { Transcript } from "./components/Transcript";
import { useChat } from "./state/useChat";

export default function App() {
  const { state, session, ask, stop, switchRole } = useChat();

  return (
    <div className="app">
      <header className="masthead">
        <h1>Ledger</h1>
        <span className="tagline">streaming chat over governed data</span>
        <span className="spacer" />
        {session && (
          <RoleSwitcher
            role={session.role}
            disabled={state.streaming}
            onChange={switchRole}
          />
        )}
      </header>

      {state.demoMode && (
        <div className="demo-banner" data-testid="demo-banner">
          Demo mode — answers come from a scripted model, not a live one. The tool calls,
          governance events, and query results below are all real.
        </div>
      )}

      <Transcript turns={state.turns} onSuggestion={ask} />
      <Composer streaming={state.streaming} onAsk={ask} onStop={stop} />
    </div>
  );
}
