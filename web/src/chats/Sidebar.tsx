import { useState } from "react";
import type { Account, ConversationSummary } from "../api/account";

export function Sidebar({
  account,
  conversations,
  activeId,
  showArchived,
  onNew,
  onSelect,
  onRename,
  onArchive,
  onDelete,
  onToggleArchived,
  onOpenSettings,
  onSignOut,
}: {
  account: Account;
  conversations: ConversationSummary[];
  activeId: string | null;
  showArchived: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onArchive: (id: string, archived: boolean) => void;
  onDelete: (id: string) => void;
  onToggleArchived: () => void;
  onOpenSettings: () => void;
  onSignOut: () => void;
}) {
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const startRename = (conversation: ConversationSummary) => {
    setRenaming(conversation.id);
    setDraft(conversation.title);
    setMenuFor(null);
  };

  const commitRename = (id: string) => {
    const title = draft.trim();
    if (title) onRename(id, title);
    setRenaming(null);
  };

  const confirmDelete = (conversation: ConversationSummary) => {
    setMenuFor(null);
    // The one place this app must not be vague. Deleting the transcript does
    // not delete the governance record, and a confirmation that implied
    // otherwise would be the single place the project lies about itself.
    const confirmed = window.confirm(
      `Delete “${conversation.title}”?\n\n` +
        "The transcript is removed permanently.\n\n" +
        "The audit log is not: every tool call this chat made stays recorded " +
        "and visible under /api/audit. That record belongs to the " +
        "organisation, not to the conversation.",
    );
    if (confirmed) onDelete(conversation.id);
  };

  return (
    <aside className="sidebar" data-testid="sidebar">
      <button type="button" className="new-chat" onClick={onNew} data-testid="new-chat">
        <span aria-hidden>＋</span> New chat
      </button>

      <div className="chat-list" data-testid="chat-list">
        {conversations.length === 0 ? (
          <p className="chat-empty">
            {showArchived ? "Nothing archived." : "No chats yet."}
          </p>
        ) : (
          conversations.map((conversation) => (
            <div
              key={conversation.id}
              className={`chat-item ${conversation.id === activeId ? "active" : ""}`}
              data-testid="chat-item"
              data-id={conversation.id}
            >
              {renaming === conversation.id ? (
                <input
                  autoFocus
                  className="rename-input"
                  value={draft}
                  data-testid="rename-input"
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => commitRename(conversation.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename(conversation.id);
                    if (e.key === "Escape") setRenaming(null);
                  }}
                />
              ) : (
                <>
                  <button
                    type="button"
                    className="chat-title"
                    onClick={() => onSelect(conversation.id)}
                    title={conversation.title}
                  >
                    {conversation.title}
                  </button>
                  <button
                    type="button"
                    className="chat-menu-toggle"
                    aria-label={`Actions for ${conversation.title}`}
                    data-testid="chat-menu"
                    onClick={() => setMenuFor(menuFor === conversation.id ? null : conversation.id)}
                  >
                    ⋯
                  </button>
                </>
              )}

              {menuFor === conversation.id && (
                <div className="chat-menu" role="menu">
                  <button type="button" onClick={() => startRename(conversation)}>
                    Rename
                  </button>
                  <button
                    type="button"
                    data-testid="archive-chat"
                    onClick={() => {
                      onArchive(conversation.id, !conversation.archived);
                      setMenuFor(null);
                    }}
                  >
                    {conversation.archived ? "Unarchive" : "Archive"}
                  </button>
                  <button
                    type="button"
                    className="danger"
                    data-testid="delete-chat"
                    onClick={() => confirmDelete(conversation)}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
          ))
        )}
      </div>

      <button type="button" className="archive-toggle" onClick={onToggleArchived}>
        {showArchived ? "← Active chats" : "Archived"}
      </button>

      <div className="sidebar-account">
        <button
          type="button"
          className="account-button"
          onClick={onOpenSettings}
          data-testid="open-settings"
        >
          {account.avatar_url ? (
            <img src={account.avatar_url} alt="" className="avatar" />
          ) : (
            <span className="avatar placeholder" aria-hidden>
              {account.display_name.slice(0, 1).toUpperCase()}
            </span>
          )}
          <span className="account-name">
            {account.display_name}
            <span className="account-role">{account.role}</span>
          </span>
        </button>
        <button type="button" className="sign-out" onClick={onSignOut} data-testid="sign-out">
          Sign out
        </button>
      </div>
    </aside>
  );
}
