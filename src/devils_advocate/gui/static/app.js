/* Devil's Advocate GUI — Client-side JS */

const dvad = {
    _sseSource: null,
    _revisionContent: '',
    _pendingRoles: {},
    _sortState: { col: null, asc: true },

    // ── Vendor icon mapping ──────────────────────────────────────────
    VENDOR_ICONS: {
        'Anthropic': 'gem',
        'OpenAI': 'sparkles',
        'Google': 'globe',
        'xAI': 'zap',
        'DeepSeek': 'compass',
        'Moonshot': 'moon',
        'MiniMax': 'box',
    },

    // ── Table row click navigation ───────────────────────────────────
    init() {
        document.querySelectorAll('.clickable-row').forEach(row => {
            row.addEventListener('click', () => {
                window.location.href = row.dataset.href;
            });
        });

        // Project filter (Project is now column 0)
        const filter = document.getElementById('project-filter');
        if (filter) {
            filter.addEventListener('input', () => {
                const val = filter.value.toLowerCase();
                document.querySelectorAll('#reviews-table tbody tr').forEach(tr => {
                    const project = tr.children[0]?.textContent?.toLowerCase() || '';
                    tr.style.display = project.includes(val) ? '' : 'none';
                });
            });
        }

        // Override buttons
        document.querySelectorAll('.override-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.overrideGroup(btn.dataset.group, btn.dataset.action);
            });
        });

        this.initSorting();
        this.initRolePills();
        this.initNewReviewForm();
        this.initTimeoutEditing();
        this.initMaxTokenEditing();
        this.initThinkingToggle();
        this.initSettingsToggle();
    },

    // ── New Review Form (Dashboard) ─────────────────────────────────
    _formData: null,

    initNewReviewForm() {
        const form = document.getElementById('review-form');
        if (!form) return;

        const modeCards = document.querySelectorAll('.mode-card input[name="mode"]');
        const hiddenMode = document.getElementById('review-mode');

        // Sync mode cards to hidden field
        modeCards.forEach(radio => {
            radio.addEventListener('change', () => {
                hiddenMode.value = radio.value;
                this.updateModeUI();
            });
        });
        this.updateModeUI();

        // Form submit → show interstitial
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            this._formData = new FormData(form);
            this.showInterstitial();
        });
    },

    updateModeUI() {
        const mode = document.querySelector('input[name="mode"]:checked')?.value || 'plan';
        const inputFilesInput = document.getElementById('input_files');
        const specRow = document.getElementById('spec-row');
        const projectDirRow = document.getElementById('project-dir-row');
        const fileHint = document.getElementById('file-hint');
        const inputRow = document.getElementById('input-files-row');

        if (!inputFilesInput) return;

        if (mode === 'plan') {
            inputFilesInput.multiple = true;
            inputFilesInput.required = true;
            specRow.style.display = 'none';
            projectDirRow.style.display = 'none';
            fileHint.textContent = 'Plan file + up to 5 reference files';
            inputRow.style.display = '';
        } else if (mode === 'code') {
            inputFilesInput.multiple = false;
            inputFilesInput.required = true;
            specRow.style.display = '';
            projectDirRow.style.display = 'none';
            fileHint.textContent = 'Exactly one file required for code mode';
            inputRow.style.display = '';
        } else if (mode === 'spec') {
            inputFilesInput.multiple = true;
            inputFilesInput.required = true;
            specRow.style.display = 'none';
            projectDirRow.style.display = 'none';
            fileHint.textContent = 'Specification file(s) to enrich with suggestions';
            inputRow.style.display = '';
        } else {
            // integration
            inputFilesInput.multiple = true;
            inputFilesInput.required = false;
            specRow.style.display = '';
            projectDirRow.style.display = '';
            fileHint.textContent = 'Input files are optional for integration mode';
            inputRow.style.display = '';
        }
    },

    buildCommand() {
        const mode = document.getElementById('review-mode')?.value || 'plan';
        const project = document.getElementById('project')?.value || '';
        const binary = (typeof dvadBinary !== 'undefined') ? dvadBinary : 'dvad';

        let parts = [binary, 'review', '--mode', mode, '--project', project];

        const inputFiles = document.getElementById('input_files')?.files || [];
        for (let i = 0; i < inputFiles.length; i++) {
            parts.push('--input', inputFiles[i].name);
        }

        const specFile = document.getElementById('spec_file')?.files?.[0];
        if (specFile) {
            parts.push('--spec', specFile.name);
        }

        const projectDir = document.getElementById('project_dir')?.value?.trim();
        if (projectDir) {
            parts.push('--project-dir', projectDir);
        }

        const maxCost = document.getElementById('max_cost')?.value?.trim();
        if (maxCost) {
            parts.push('--max-cost', maxCost);
        }

        if (document.getElementById('dry_run')?.checked) {
            parts.push('--dry-run');
        }

        return parts.join(' \\\n    ');
    },

    showInterstitial() {
        const cmd = this.buildCommand();
        document.getElementById('command-preview-text').textContent = cmd;
        document.getElementById('interstitial').style.display = '';
        document.querySelector('.review-settings').style.display = 'none';
        document.querySelector('.mode-cards').style.display = 'none';
    },

    cancelInterstitial() {
        document.getElementById('interstitial').style.display = 'none';
        document.querySelector('.review-settings').style.display = '';
        document.querySelector('.mode-cards').style.display = '';
    },

    copyCommand() {
        const text = document.getElementById('command-preview-text')?.textContent || '';
        navigator.clipboard.writeText(text);
    },

    executeReview() {
        if (!this._formData) return;

        const runBtn = document.getElementById('run-review-btn');
        const errorDiv = document.getElementById('form-error');
        if (runBtn) { runBtn.disabled = true; runBtn.textContent = 'Starting...'; }
        if (errorDiv) errorDiv.style.display = 'none';

        const token = document.querySelector('meta[name="csrf-token"]')?.content || '';

        fetch('/api/review/start', {
            method: 'POST',
            headers: {'X-DVAD-Token': token},
            body: this._formData,
        })
        .then(r => r.json().then(data => ({ok: r.ok, data})))
        .then(({ok, data}) => {
            if (ok && data.review_id) {
                window.location.href = '/review/' + data.review_id;
            } else {
                if (errorDiv) {
                    errorDiv.textContent = data.detail || 'Failed to start review';
                    errorDiv.style.display = '';
                }
                if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Review'; }
                this.cancelInterstitial();
            }
        })
        .catch(err => {
            if (errorDiv) {
                errorDiv.textContent = 'Network error: ' + err.message;
                errorDiv.style.display = '';
            }
            if (runBtn) { runBtn.disabled = false; runBtn.textContent = 'Run Review'; }
            this.cancelInterstitial();
        });
    },

    // ── CSRF token ───────────────────────────────────────────────────
    getToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    },

    // ── Column sorting ───────────────────────────────────────────────
    initSorting() {
        const headers = document.querySelectorAll('.th-sortable');
        if (!headers.length) return;

        headers.forEach(th => {
            th.addEventListener('click', () => {
                const col = parseInt(th.dataset.col);
                const type = th.dataset.sortType || 'string';
                const asc = this._sortState.col === col ? !this._sortState.asc : true;
                this._sortState = { col, asc };

                // Update indicators
                headers.forEach(h => {
                    const ind = h.querySelector('.sort-indicator');
                    if (ind) ind.textContent = '';
                });
                const indicator = th.querySelector('.sort-indicator');
                if (indicator) indicator.textContent = asc ? ' \u25B2' : ' \u25BC';

                // Sort rows
                const tbody = document.querySelector('#reviews-table tbody');
                if (!tbody) return;
                const rows = Array.from(tbody.querySelectorAll('tr.clickable-row'));

                rows.sort((a, b) => {
                    const aVal = a.children[col]?.dataset.sortVal || '';
                    const bVal = b.children[col]?.dataset.sortVal || '';

                    let cmp;
                    if (type === 'numeric') {
                        cmp = (parseFloat(aVal) || 0) - (parseFloat(bVal) || 0);
                    } else {
                        cmp = aVal.localeCompare(bVal);
                    }
                    return asc ? cmp : -cmp;
                });

                rows.forEach(row => tbody.appendChild(row));
            });
        });
    },

    // ── Override group ───────────────────────────────────────────────
    async overrideGroup(groupId, resolution) {
        const card = document.getElementById('group-' + groupId);
        if (!card) return;

        const buttons = card.querySelectorAll('.override-btn');
        buttons.forEach(b => b.disabled = true);

        try {
            const reviewId = window.location.pathname.split('/review/')[1];
            const resp = await fetch(`/api/review/${reviewId}/override`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ group_id: groupId, resolution }),
            });

            const data = await resp.json();
            if (resp.ok) {
                card.classList.add('resolved');
                const actions = card.querySelector('.card-actions');
                if (actions) {
                    const label = {
                        'overridden': 'Accepted (Reviewer)',
                        'auto_dismissed': 'Accepted (Author)',
                        'escalated': 'Kept Open'
                    }[resolution] || resolution;
                    actions.innerHTML = `<span class="dim">Resolution: ${label}</span>`;
                }
                this._checkAllOverridesResolved();
            } else {
                alert(data.detail || 'Override failed');
                buttons.forEach(b => b.disabled = false);
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            buttons.forEach(b => b.disabled = false);
        }
    },

    // ── SSE Progress ─────────────────────────────────────────────────
    connectSSE(reviewId) {
        const logOutput = document.getElementById('log-output');
        const source = new EventSource(`/api/review/${reviewId}/progress`);
        this._sseSource = source;

        const seenPhases = new Set();

        source.onmessage = (e) => {
            try {
                const ev = JSON.parse(e.data);

                // Handle metadata event (role→model mapping for cost table)
                if (ev.type === 'metadata') {
                    this._handleMetadata(ev.detail);
                    return;
                }

                // Handle cost event (update cost table, suppress from log)
                if (ev.type === 'cost') {
                    this._handleCostUpdate(ev.detail);
                    return;
                }

                // Append to log (all other events with a message)
                if (ev.message && logOutput) {
                    const line = document.createElement('div');
                    const ts = ev.timestamp ? `[${ev.timestamp}] ` : '';
                    line.textContent = ts + ev.message;
                    logOutput.appendChild(line);
                    const scrollParent = logOutput.parentElement;
                    if (scrollParent) scrollParent.scrollTop = scrollParent.scrollHeight;
                }

                // Detect spec mode from the initial review_start event
                if (ev.phase === 'review_start' && ev.message && ev.message.includes('spec review')) {
                    this._specMode = true;
                    document.querySelectorAll('.phase-adversarial').forEach(el => {
                        el.style.display = 'none';
                    });
                    document.querySelectorAll('.bc-spec-only').forEach(el => {
                        el.classList.remove('bc-spec-only');
                    });
                    const reviewersLabel = document.getElementById('reviewers-phase-label');
                    if (reviewersLabel) reviewersLabel.textContent = 'REVIEWERS';
                    const finalLabel = document.getElementById('final-phase-label');
                    if (finalLabel) finalLabel.textContent = 'SUGGESTIONS';
                }

                // Update phase dots
                if (ev.phase) {
                    this._updatePhase(ev.phase, seenPhases);
                }

                // Terminal events
                if (ev.type === 'complete') {
                    source.close();
                    setTimeout(() => window.location.reload(), 500);
                } else if (ev.type === 'error') {
                    source.close();
                    if (logOutput) {
                        const errLine = document.createElement('div');
                        errLine.style.color = '#ff4757';
                        errLine.textContent = 'ERROR: ' + (ev.message || 'Review failed');
                        logOutput.appendChild(errLine);
                    }
                }
            } catch (parseErr) {
                // Ignore parse errors (e.g., keepalive pings)
            }
        };

        source.onerror = () => {
            source.close();
            setTimeout(() => window.location.reload(), 3000);
        };
    },

    _handleMetadata(detail) {
        if (!detail) return;

        // Set mode label
        const modeLabel = document.getElementById('review-mode-label');
        if (modeLabel && detail.mode) {
            modeLabel.textContent = detail.mode + ' review';
        }

        // Build cost table from roles
        const table = document.getElementById('live-cost-table');
        if (!table || !detail.roles) return;

        const roles = detail.roles;
        let html = '';
        for (const [role, model] of Object.entries(roles)) {
            const label = role.replace(/_/g, ' ');
            html += `<div class="cost-row">` +
                `<span class="cost-role">${label}:</span>` +
                `<span class="cost-model">${model}</span>` +
                `<span class="cost-value" id="cost-${role}">$0.000000</span>` +
                `</div>`;
        }
        html += `<div class="cost-row cost-divider"></div>`;
        html += `<div class="cost-row">` +
            `<span class="cost-role"></span>` +
            `<span class="cost-model"></span>` +
            `<span class="cost-value cost-total-value" id="cost-total">$0.000000</span>` +
            `</div>`;
        table.innerHTML = html;

        // Spec mode: hide adversarial phases, show spec-only steps
        if (detail.mode === 'spec') {
            this._specMode = true;
            document.querySelectorAll('.phase-adversarial').forEach(el => {
                el.style.display = 'none';
            });
            document.querySelectorAll('.bc-spec-only').forEach(el => {
                el.classList.remove('bc-spec-only');
            });
            const reviewersLabel = document.getElementById('reviewers-phase-label');
            if (reviewersLabel) reviewersLabel.textContent = 'REVIEWERS';
            const finalLabel = document.getElementById('final-phase-label');
            if (finalLabel) finalLabel.textContent = 'SUGGESTIONS';
        }
    },

    _roleCosts: {},

    _handleCostUpdate(detail) {
        if (!detail) return;
        const role = detail.role;
        const callCost = parseFloat(detail.cost) || 0;

        // Accumulate per-role cost
        this._roleCosts[role] = (this._roleCosts[role] || 0) + callCost;

        const costEl = document.getElementById('cost-' + role);
        if (costEl) {
            costEl.textContent = '$' + this._roleCosts[role].toFixed(6);
        }
        const totalEl = document.getElementById('cost-total');
        if (totalEl) {
            totalEl.textContent = '$' + parseFloat(detail.total).toFixed(6);
        }
    },

    _updatePhase(phase, seenPhases) {
        const phaseMap = {
            'review_start': 'dot-reviewers',
            'round1_calling': 'dot-reviewers',
            'round1_responded': 'dot-reviewers',
            'normalization': 'dot-reviewers',
            'dedup_calling': 'dot-dedup',
            'dedup_responded': 'dot-dedup',
            'round1_author': 'dot-author',
            'round2_skip': 'dot-rebuttals',
            'round2_skip_reviewer': 'dot-rebuttals',
            'round2_skip_context': 'dot-rebuttals',
            'round2_rebuttal_failed': 'dot-rebuttals',
            'round2_author_failed': 'dot-rebuttals',
            'governance_catastrophic': 'dot-governance',
            'governance_complete': 'dot-governance',
            'cost_warning': null,
            'cost_exceeded': null,
            'revision_calling': 'dot-revision',
            'revision_responded': 'dot-revision',
            'revision_skip': 'dot-revision',
            'revision_skip_context': 'dot-revision',
            'revision_extraction_failed': 'dot-revision',
            'revision_failed': 'dot-revision',
        };

        const dotId = phaseMap[phase];
        if (!dotId) return;

        const fullOrder = ['dot-reviewers', 'dot-dedup', 'dot-author', 'dot-rebuttals', 'dot-governance', 'dot-revision'];
        const dotOrder = fullOrder.filter(id => document.getElementById(id));
        const currentIdx = dotOrder.indexOf(dotId);

        for (let i = 0; i < dotOrder.length; i++) {
            const dot = document.getElementById(dotOrder[i]);
            if (!dot) continue;
            if (i < currentIdx) {
                dot.innerHTML = '&#9679;';
                dot.className = 'phase-dot done';
            } else if (i === currentIdx) {
                dot.innerHTML = '&#9679;';
                dot.className = 'phase-dot active';
            }
        }
    },

    _checkAllOverridesResolved() {
        const cards = document.querySelectorAll('.group-card.card-escalated');
        if (!cards.length) return;
        const allResolved = Array.from(cards).every(c => c.classList.contains('resolved'));
        if (allResolved) {
            // Mark overrides pipeline step as done
            const overrideStep = document.getElementById('pipe-overrides');
            if (overrideStep) {
                overrideStep.className = 'pipeline-step done';
            }
            // Show banner
            const banner = document.getElementById('overrides-banner');
            if (banner) banner.classList.add('visible');
            // Highlight revision button
            const reviseBtn = document.getElementById('revise-btn');
            if (reviseBtn) {
                reviseBtn.classList.add('btn-accent');
                reviseBtn.style.animation = 'step-pulse 2s ease-in-out 3';
            }
        }
    },

    // ── Revision ─────────────────────────────────────────────────────
    async startRevision(reviewId) {
        const btn = document.getElementById('revise-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Generating...';
        }

        try {
            const resp = await fetch(`/api/review/${reviewId}/revise`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
            });

            const data = await resp.json();
            if (resp.ok && data.content) {
                this._revisionContent = data.content;
                const output = document.getElementById('revision-output');
                const content = document.getElementById('revision-content');
                const cost = document.getElementById('revision-cost');
                if (output && content) {
                    content.textContent = data.content;
                    output.style.display = '';
                }
                if (cost) {
                    cost.textContent = `Cost: $${(data.cost || 0).toFixed(6)}`;
                }

                // Hide generate button, add download link
                if (btn) btn.style.display = 'none';
                const footer = document.querySelector('.footer-actions');
                if (footer && !footer.querySelector('.download-revised-link')) {
                    const link = document.createElement('a');
                    link.href = `/api/review/${reviewId}/revised`;
                    link.className = 'btn btn-green download-revised-link';
                    link.textContent = 'Download Revised';
                    footer.insertBefore(link, footer.firstChild);
                }
                // Mark revision pipeline step as done
                const revisionStep = document.getElementById('pipe-revision');
                if (revisionStep) {
                    revisionStep.className = 'pipeline-step done';
                }
            } else {
                alert(data.detail || data.message || 'Revision failed');
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Generate Revision';
                }
            }
        } catch (err) {
            alert('Network error: ' + err.message);
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Generate Revision';
            }
        }
    },

    copyRevision() {
        if (this._revisionContent) {
            navigator.clipboard.writeText(this._revisionContent);
        }
    },

    // ── Config tabs ──────────────────────────────────────────────────
    switchTab(tab) {
        document.querySelectorAll('.tab-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.tab === tab);
        });
        document.querySelectorAll('.tab-content').forEach(c => {
            c.style.display = c.id === 'tab-' + tab ? '' : 'none';
        });
    },

    async validateYaml() {
        const yaml = document.getElementById('yaml-editor')?.value || '';
        const result = document.getElementById('yaml-result');

        try {
            const resp = await fetch('/api/config/validate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml }),
            });
            const data = await resp.json();
            if (result) {
                if (data.valid) {
                    result.innerHTML = '<span style="color:#2ed573">Configuration is valid.</span>';
                } else {
                    result.innerHTML = (data.issues || []).map(([level, msg]) =>
                        `<div class="issue issue-${level}">${level.toUpperCase()}: ${msg}</div>`
                    ).join('');
                }

                // Show warnings even when valid
                if (data.valid && data.issues && data.issues.length > 0) {
                    result.innerHTML += (data.issues).map(([level, msg]) =>
                        `<div class="issue issue-${level}">${level.toUpperCase()}: ${msg}</div>`
                    ).join('');
                }
            }
        } catch (err) {
            if (result) result.textContent = 'Error: ' + err.message;
        }
    },

    async saveYaml() {
        const yaml = document.getElementById('yaml-editor')?.value || '';
        const result = document.getElementById('yaml-result');

        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml }),
            });
            const data = await resp.json();
            if (result) {
                if (resp.ok) {
                    result.innerHTML = `<span style="color:#2ed573">Saved to ${data.path}</span>`;
                } else {
                    result.innerHTML = `<span style="color:#ff4757">${data.detail}</span>`;
                }
            }
        } catch (err) {
            if (result) result.textContent = 'Error: ' + err.message;
        }
    },

    // ── Role Icons ──────────────────────────────────────────────────
    initRolePills() {
        const icons = document.querySelectorAll('.role-icon');
        if (!icons.length || this._rolePillsInitialized) return;
        this._rolePillsInitialized = true;

        icons.forEach(icon => {
            icon.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                const model = icon.dataset.model;
                const role = icon.dataset.role;
                const isActive = icon.classList.contains('role-active');

                // Radio roles: only one model per role (except reviewer which is multi)
                const radioRoles = ['author', 'deduplication', 'integration_reviewer', 'normalization', 'revision'];

                if (radioRoles.includes(role)) {
                    // Deactivate all other icons for this role
                    document.querySelectorAll(`.role-icon[data-role="${role}"]`).forEach(p => {
                        p.classList.remove('role-active');
                        p.title = p.title.replace(' (assigned)', '');
                    });
                    // Activate this one (unless toggling off)
                    if (!isActive) {
                        icon.classList.add('role-active');
                        if (!icon.title.includes('(assigned)')) {
                            icon.title = icon.title + ' (assigned)';
                        }
                    }
                } else {
                    // Checkbox role (reviewer): toggle, max 2
                    icon.classList.toggle('role-active');
                    if (icon.classList.contains('role-active')) {
                        if (!icon.title.includes('(assigned)')) {
                            icon.title = icon.title + ' (assigned)';
                        }
                        // Cap at 2 reviewers: drop the oldest if over limit
                        const active = Array.from(document.querySelectorAll('.role-icon[data-role="reviewer"].role-active'));
                        if (active.length > 2) {
                            const oldest = active.find(el => el !== icon);
                            if (oldest) {
                                oldest.classList.remove('role-active');
                                oldest.title = oldest.title.replace(' (assigned)', '');
                            }
                        }
                    } else {
                        icon.title = icon.title.replace(' (assigned)', '');
                    }
                }

                this._markRolesDirty();
            });
        });
        this._updateRoleSummary();
    },

    _markRolesDirty() {
        const toast = document.getElementById('save-roles-toast');
        if (toast) toast.classList.add('visible');
        this._updateRoleSummary();
    },

    _updateRoleSummary() {
        const summary = document.getElementById('role-summary');
        if (!summary) return;

        // Scan all active role icons
        const roles = {};
        document.querySelectorAll('.role-icon.role-active').forEach(pill => {
            const model = pill.dataset.model;
            const role = pill.dataset.role;
            if (role === 'reviewer') {
                if (!roles.reviewers) roles.reviewers = [];
                roles.reviewers.push(model);
            } else {
                roles[role] = model;
            }
        });

        // Update single-assignment roles
        const singleRoles = ['author', 'deduplication', 'normalization', 'revision', 'integration_reviewer'];
        singleRoles.forEach(role => {
            const el = document.getElementById('rs-' + role);
            if (!el) return;
            if (roles[role]) {
                el.textContent = roles[role];
                el.className = 'role-summary-value';
            } else {
                el.textContent = 'unassigned';
                el.className = 'role-summary-value unassigned';
            }
        });

        // Update reviewers
        const rv1 = document.getElementById('rs-reviewer1');
        const rv2 = document.getElementById('rs-reviewer2');
        if (rv1) {
            if (roles.reviewers && roles.reviewers[0]) {
                rv1.textContent = roles.reviewers[0];
                rv1.className = 'role-summary-value';
            } else {
                rv1.textContent = 'unassigned';
                rv1.className = 'role-summary-value unassigned';
            }
        }
        if (rv2) {
            if (roles.reviewers && roles.reviewers[1]) {
                rv2.textContent = roles.reviewers[1];
                rv2.className = 'role-summary-value';
            } else {
                rv2.textContent = '\u2014';
                rv2.className = 'role-summary-value';
            }
        }

        // Update vendor icons + brain icons in role summary
        this._updateRoleSummaryIcons(roles);

        // Update thinking icon eligibility on model cards
        this._updateThinkingEligibility();
    },

    _updateRoleSummaryIcons(roles) {
        if (typeof modelVendors === 'undefined' || typeof modelThinking === 'undefined') return;

        const roleKeys = [
            { key: 'author', prefix: 'author' },
            { key: 'reviewer1', prefix: 'reviewer1', get: (r) => r.reviewers?.[0] },
            { key: 'reviewer2', prefix: 'reviewer2', get: (r) => r.reviewers?.[1] },
            { key: 'deduplication', prefix: 'deduplication' },
            { key: 'normalization', prefix: 'normalization' },
            { key: 'revision', prefix: 'revision' },
            { key: 'integration_reviewer', prefix: 'integration_reviewer' },
        ];

        roleKeys.forEach(({ key, prefix, get }) => {
            const model = get ? get(roles) : roles[key];
            const iconEl = document.getElementById('rsi-' + prefix);
            const cotEl = document.getElementById('rsc-' + prefix);

            if (iconEl) {
                if (model && modelVendors[model]) {
                    const iconName = this.VENDOR_ICONS[modelVendors[model]] || 'cpu';
                    iconEl.innerHTML = `<i data-lucide="${iconName}"></i>`;
                    iconEl.title = modelVendors[model];
                } else {
                    iconEl.innerHTML = '<i data-lucide="cpu"></i>';
                    iconEl.title = '';
                }
            }
            if (cotEl) {
                if (model && modelThinking[model]) {
                    cotEl.classList.add('cot-active');
                } else {
                    cotEl.classList.remove('cot-active');
                }
            }
        });

        // Re-render lucide icons
        if (typeof lucide !== 'undefined') lucide.createIcons();
    },

    _updateThinkingEligibility() {
        document.querySelectorAll('.thinking-icon').forEach(icon => {
            const model = icon.dataset.model;
            const hasActiveRole = document.querySelector(`.role-icon.role-active[data-model="${model}"]`);
            if (hasActiveRole) {
                icon.classList.add('thinking-eligible');
            } else {
                icon.classList.remove('thinking-eligible');
                icon.classList.remove('thinking-active');
            }
        });
    },

    async saveRoleAssignments() {
        const editor = document.getElementById('yaml-editor');
        if (!editor || typeof jsyaml === 'undefined') {
            alert('YAML editor or js-yaml library not available. Switch to Raw YAML tab to edit roles.');
            return;
        }

        let config;
        try {
            config = jsyaml.load(editor.value);
        } catch (e) {
            alert('Failed to parse current YAML: ' + e.message);
            return;
        }

        // Build roles from active pills
        const roles = {};
        const reviewers = [];

        document.querySelectorAll('.role-icon.role-active').forEach(icon => {
            const model = icon.dataset.model;
            const role = icon.dataset.role;

            if (role === 'reviewer') {
                reviewers.push(model);
            } else {
                roles[role] = model;
            }
        });

        if (reviewers.length > 0) {
            roles.reviewers = reviewers;
        }

        config.roles = roles;

        // Serialize and save
        const newYaml = jsyaml.dump(config, { lineWidth: -1, noRefs: true });
        editor.value = newYaml;

        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-DVAD-Token': this.getToken(),
                },
                body: JSON.stringify({ yaml: newYaml }),
            });
            const data = await resp.json();

            const toast = document.getElementById('save-roles-toast');
            if (toast) toast.classList.remove('visible');

            const vr = document.getElementById('validation-result');
            if (resp.ok) {
                if (vr) vr.innerHTML = '<div class="issue issue-ok">Roles saved. Configuration is valid.</div>';
            } else {
                if (vr) vr.innerHTML = `<div class="issue issue-error">ERROR: ${data.detail}</div>`;
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }
    },

    // ── Inline Timeout Editing ───────────────────────────────────────
    initTimeoutEditing() {
        document.querySelectorAll('.editable-timeout').forEach(span => {
            span.addEventListener('click', () => {
                if (span.querySelector('input')) return; // Already editing
                const currentVal = span.textContent.trim();
                const modelName = span.dataset.model;

                const input = document.createElement('input');
                input.type = 'number';
                input.min = '10';
                input.max = '7200';
                input.value = currentVal;
                input.className = 'timeout-input';

                span.textContent = '';
                span.appendChild(input);
                input.focus();
                input.select();

                const commit = async () => {
                    const newVal = parseInt(input.value);
                    if (isNaN(newVal) || newVal < 10 || newVal > 7200) {
                        span.textContent = currentVal;
                        return;
                    }

                    span.textContent = newVal;

                    try {
                        const resp = await fetch('/api/config/model-timeout', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-DVAD-Token': this.getToken(),
                            },
                            body: JSON.stringify({ model_name: modelName, timeout: newVal }),
                        });
                        if (!resp.ok) {
                            const data = await resp.json();
                            alert(data.detail || 'Failed to update timeout');
                            span.textContent = currentVal;
                        }
                    } catch (err) {
                        alert('Network error: ' + err.message);
                        span.textContent = currentVal;
                    }
                };

                input.addEventListener('blur', commit);
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        input.blur();
                    } else if (e.key === 'Escape') {
                        span.textContent = currentVal;
                    }
                });
            });
        });
    },

    // ── Inline Max Token Editing ────────────────────────────────────
    initMaxTokenEditing() {
        document.querySelectorAll('.editable-max-tokens').forEach(span => {
            span.addEventListener('click', () => {
                if (span.querySelector('input')) return;
                const currentVal = span.textContent.trim();
                const modelName = span.dataset.model;

                const input = document.createElement('input');
                input.type = 'number';
                input.min = '1';
                input.max = '1000000';
                input.value = currentVal === 'unset' ? '' : currentVal;
                input.className = 'timeout-input';
                input.placeholder = 'unset';

                span.textContent = '';
                span.appendChild(input);
                input.focus();
                input.select();

                const commit = async () => {
                    const rawVal = input.value.trim();
                    if (rawVal === '') {
                        span.textContent = 'unset';
                        // Send null to clear
                        try {
                            await fetch('/api/config/model-max-tokens', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-DVAD-Token': this.getToken(),
                                },
                                body: JSON.stringify({ model_name: modelName, max_output_tokens: null }),
                            });
                        } catch (err) { /* ignore */ }
                        return;
                    }
                    const newVal = parseInt(rawVal);
                    if (isNaN(newVal) || newVal < 1 || newVal > 1000000) {
                        span.textContent = currentVal;
                        return;
                    }

                    span.textContent = newVal;

                    try {
                        const resp = await fetch('/api/config/model-max-tokens', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-DVAD-Token': this.getToken(),
                            },
                            body: JSON.stringify({ model_name: modelName, max_output_tokens: newVal }),
                        });
                        if (!resp.ok) {
                            const data = await resp.json();
                            alert(data.detail || 'Failed to update max output tokens');
                            span.textContent = currentVal;
                        }
                    } catch (err) {
                        alert('Network error: ' + err.message);
                        span.textContent = currentVal;
                    }
                };

                input.addEventListener('blur', commit);
                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        input.blur();
                    } else if (e.key === 'Escape') {
                        span.textContent = currentVal;
                    }
                });
            });
        });
    },

    // ── Thinking Toggle ─────────────────────────────────────────────
    initThinkingToggle() {
        document.querySelectorAll('.thinking-icon').forEach(el => {
            el.addEventListener('click', async (e) => {
                e.stopPropagation();
                const model = el.dataset.model;

                // Only allow toggle if model has an active role
                if (!el.classList.contains('thinking-eligible') && !el.classList.contains('thinking-active')) return;

                const currentlyOn = el.classList.contains('thinking-active');
                const newValue = !currentlyOn;

                const resp = await fetch('/api/config/model-thinking', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-DVAD-Token': dvad.getToken(),
                    },
                    body: JSON.stringify({ model_name: model, thinking: newValue }),
                });

                if (resp.ok) {
                    el.classList.toggle('thinking-active', newValue);
                    el.title = 'Chain of Thought: ' + (newValue ? 'enabled' : 'disabled');
                    // Update modelThinking map if available
                    if (typeof modelThinking !== 'undefined') {
                        modelThinking[model] = newValue;
                    }
                    // Update brain icons in role summary
                    this._updateRoleSummary();
                }
            });
        });
    },

    // ── Settings Toggle ──────────────────────────────────────────────
    initSettingsToggle() {
        document.querySelectorAll('.settings-toggle').forEach(el => {
            el.addEventListener('click', async (e) => {
                e.stopPropagation();
                const key = el.dataset.key;
                const currentlyOn = el.classList.contains('settings-on');
                const newValue = !currentlyOn;

                const resp = await fetch('/api/config/settings-toggle', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-DVAD-Token': dvad.getToken(),
                    },
                    body: JSON.stringify({ key, value: newValue }),
                });

                if (resp.ok) {
                    el.classList.toggle('settings-on', newValue);
                    el.textContent = newValue ? 'enabled' : 'disabled';
                }
            });
        });
    },
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => dvad.init());
