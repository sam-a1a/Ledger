import type { Role } from "../state/types";

const ROLES: Role[] = ["analyst", "viewer"];

export function RoleSwitcher({
  role,
  disabled,
  onChange,
}: {
  role: Role;
  disabled: boolean;
  onChange: (role: Role) => void;
}) {
  return (
    <div className="role-switcher" role="group" aria-label="Role">
      {ROLES.map((candidate) => (
        <button
          key={candidate}
          type="button"
          aria-pressed={role === candidate}
          disabled={disabled}
          data-testid={`role-${candidate}`}
          onClick={() => onChange(candidate)}
        >
          {candidate}
        </button>
      ))}
    </div>
  );
}
