// Boolean switch styled as a small toggle.
export function Switch(props: {
    checked: boolean;
    onChange: (checked: boolean) => void;
    label?: string;
    id?: string;
}) {
    const { checked, onChange, label, id } = props;
    return (
        <label className="flex cursor-pointer items-center gap-2" htmlFor={id}>
            <button
                id={id}
                type="button"
                role="switch"
                aria-checked={checked}
                onClick={() => onChange(!checked)}
                className={`relative h-5 w-9 rounded-full transition-colors ${
                    checked ? 'bg-brand' : 'bg-bg-muted'
                }`}
            >
                <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                        checked ? 'left-4.5' : 'left-0.5'
                    }`}
                />
            </button>
            {label ? <span className="text-sm text-text-primary">{label}</span> : null}
        </label>
    );
}
