// In-module tab strip (AgentStudio FusionTabBar equivalent). Horizontally
// scrollable so all tabs stay reachable in the embedded module iframe.
export interface TabItem {
    key: string;
    label: string;
}

export function Tabs(props: {
    tabs: TabItem[];
    active: string;
    onChange: (key: string) => void;
}) {
    const { tabs, active, onChange } = props;
    return (
        <div className="flex gap-1 overflow-x-auto border-b border-border px-4 pt-2">
            {tabs.map((tab) => (
                <button
                    key={tab.key}
                    type="button"
                    onClick={() => onChange(tab.key)}
                    className={`whitespace-nowrap rounded-t-md px-3 py-2 text-sm ${
                        active === tab.key
                            ? 'border-b-2 border-brand text-brand'
                            : 'text-text-secondary hover:text-text-primary'
                    }`}
                >
                    {tab.label}
                </button>
            ))}
        </div>
    );
}
