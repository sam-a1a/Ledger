import "./styles/tokens.css";
import "./App.css";

import { useState } from "react";
import { AuthPage } from "./auth/AuthPage";
import { SettingsPage } from "./auth/SettingsPage";
import { useSession } from "./auth/useSession";
import { Sidebar } from "./chats/Sidebar";
import { Composer } from "./components/Composer";
import { Transcript } from "./components/Transcript";
import { useChat } from "./state/useChat";

export default function App() {
  const { session, checking, setSession, updateAccount, signOut } = useSession();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const chat = useChat(session?.token ?? null);

  if (checking) {
    return (
      <div className="booting" data-testid="booting">
        <span>Ledger</span>
      </div>
    );
  }

  if (!session) return <AuthPage onSignedIn={setSession} />;

  return (
    <div className="app">
      <Sidebar
        account={session.account}
        conversations={chat.conversations}
        activeId={chat.state.conversationId}
        showArchived={chat.showArchived}
        onNew={chat.newChat}
        onSelect={(id) => void chat.openConversation(id)}
        onRename={(id, title) => void chat.rename(id, title)}
        onArchive={(id, archived) => void chat.archive(id, archived)}
        onDelete={(id) => void chat.remove(id)}
        onToggleArchived={chat.toggleArchived}
        onOpenSettings={() => setSettingsOpen(true)}
        onSignOut={signOut}
      />

      <main className="main">
        <header className="masthead">
          <h1>Ledger</h1>
          <span className="tagline">streaming chat over governed data</span>
          <span className="spacer" />
          <span className="role-badge" data-testid="role-badge">
            {session.account.role}
          </span>
        </header>

        {chat.state.demoMode && (
          <div className="demo-banner" data-testid="demo-banner">
            Demo mode — answers come from a scripted model, not a live one. The tool calls,
            governance events, and query results below are all real.
          </div>
        )}

        {settingsOpen ? (
          <SettingsPage
            token={session.token}
            account={session.account}
            onUpdated={updateAccount}
            onClose={() => setSettingsOpen(false)}
            onSignedOut={() => {
              setSettingsOpen(false);
              signOut();
            }}
          />
        ) : (
          <>
            <Transcript
              turns={chat.state.turns}
              onSuggestion={chat.ask}
              token={session.token}
              conversationId={chat.state.conversationId}
            />
            <Composer streaming={chat.state.streaming} onAsk={chat.ask} onStop={chat.stop} />
          </>
        )}
      </main>
    </div>
  );
}
