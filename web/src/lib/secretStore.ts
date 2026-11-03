// #554: session-only secret store for the canvas inspector.
// Web has no Keychain — secrets follow the repo's session-injection
// discipline (config-opti 1028): held in module memory for the page
// session only, never written to node config, localStorage, or the
// persisted flow JSON. A page reload clears everything.

const secrets = new Map<string, string>();

export function secretSlot(nodeId: string, key: string): string {
    return `${nodeId}::${key}`;
}

export function setSecret(nodeId: string, key: string, value: string): void {
    if (value === '') {
        secrets.delete(secretSlot(nodeId, key));
        return;
    }
    secrets.set(secretSlot(nodeId, key), value);
}

export function getSecret(nodeId: string, key: string): string {
    return secrets.get(secretSlot(nodeId, key)) ?? '';
}

export function hasSecret(nodeId: string, key: string): boolean {
    return secrets.has(secretSlot(nodeId, key));
}

export function clearNodeSecrets(nodeId: string): void {
    const prefix = `${nodeId}::`;
    for (const slot of secrets.keys()) {
        if (slot.startsWith(prefix)) secrets.delete(slot);
    }
}

export function clearAllSecrets(): void {
    secrets.clear();
}

// Config keys that must never survive into persisted JSON, whatever
// widget produced them (belt-and-braces on top of session-only storage).
export const SECRET_FIELD_KEYS: ReadonlySet<string> = new Set([
    'api_key',
    'api_token',
    'auth_token',
    'secret',
    'password',
    'access_token',
]);

export function isSecretField(key: string): boolean {
    return SECRET_FIELD_KEYS.has(key);
}
