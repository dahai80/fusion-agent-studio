import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReviewPage } from '../ReviewPage';

const rpcMock = vi.fn();
vi.mock('../../../api', () => ({ rpc: (...args: unknown[]) => rpcMock(...args) }));

const PACKS = [
    {
        key: 'architecture-contract',
        name: 'Architecture-Contract Reviewer',
        description: 'Interface consistency, drift, boundary violations.',
        blind_review: true,
        default_params: {},
    },
    {
        key: 'security',
        name: 'Security Reviewer',
        description: 'STRIDE pass, authn/authz gaps, injection.',
        blind_review: true,
        default_params: {},
    },
    {
        key: 'domain',
        name: 'Domain Reviewer',
        description: 'Domain-correctness pass.',
        blind_review: true,
        default_params: { domain_notes: '' },
    },
    {
        key: 'red-team',
        name: 'Red-Team Reviewer',
        description: 'Mandatory dissent.',
        blind_review: true,
        default_params: {},
    },
];

describe('ReviewPage (#549)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
    });

    it('lists the 4 first-party packs selectable', async () => {
        rpcMock.mockResolvedValueOnce({ packs: PACKS });
        render(<ReviewPage />);
        await waitFor(() => expect(screen.getByTestId('pack-list')).toBeDefined());
        for (const p of PACKS) {
            expect(screen.getByTestId(`pack-${p.key}`)).toBeDefined();
        }
        expect(rpcMock).toHaveBeenCalledWith('reviewer.packs', {});
    });

    it('runs selected packs in parallel and renders findings with attribution', async () => {
        rpcMock.mockImplementation((method: string) => {
            if (method === 'reviewer.packs') return Promise.resolve({ packs: PACKS });
            if (method === 'reviewer.run') {
                return Promise.resolve({
                    status: 'ok',
                    total: 2,
                    findings: [
                        {
                            severity: 'P0',
                            location: '§2 / api.py:10',
                            rationale: 'unbounded input',
                            recommendation: 'add size cap',
                            pack: 'security',
                        },
                        {
                            severity: 'P1',
                            location: '§4',
                            rationale: 'no fallback path',
                            recommendation: 'add fallback',
                            pack: 'red-team',
                        },
                    ],
                    per_pack: {
                        security: { status: 'ok', finding_count: 1 },
                        'red-team': { status: 'ok', finding_count: 1 },
                        domain: { status: 'ok', finding_count: 0 },
                        'architecture-contract': { status: 'ok', finding_count: 0 },
                    },
                });
            }
            return Promise.resolve({});
        });
        render(<ReviewPage />);
        await waitFor(() => expect(screen.getByTestId('pack-list')).toBeDefined());
        const artifact = screen.getByTestId('review-artifact') as HTMLTextAreaElement;
        await userEvent.type(artifact, 'design doc body');
        await userEvent.click(
            screen.getByRole('button', {
                name: /并行运行选中的评审包|Run selected packs in parallel/i,
            })
        );
        await waitFor(() => expect(screen.getByTestId('review-result')).toBeDefined());
        const rows = screen.getAllByTestId('finding-row');
        expect(rows).toHaveLength(2);
        expect(rows[0].textContent).toContain('P0');
        expect(rows[0].textContent).toContain('security');
        expect(screen.getByTestId('per-pack-summary').textContent).toContain('red-team: 1');
        expect(rpcMock).toHaveBeenCalledWith(
            'reviewer.run',
            expect.objectContaining({ packs: expect.anything(), artifact: 'design doc body' })
        );
    });

    it('blind-review toggle reveals author-notes field only when off', async () => {
        rpcMock.mockResolvedValueOnce({ packs: PACKS });
        render(<ReviewPage />);
        await waitFor(() => expect(screen.getByTestId('blind-toggle')).toBeDefined());
        expect(screen.queryByTestId('review-author-notes')).toBeNull();
        await userEvent.click(screen.getByTestId('blind-toggle').querySelector('input')!);
        expect(screen.getByTestId('review-author-notes')).toBeDefined();
    });
});
