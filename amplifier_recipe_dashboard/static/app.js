/**
 * Recipe Dashboard - single-class vanilla JS frontend.
 * Polls /api/sessions and /api/session/<id>/tasks for live progress.
 */
class RecipeDashboard {
    constructor() {
        this._pollInterval = null;
        this._currentSessionId = null;
        this._el = document.getElementById('app');
        this._statusDot = document.getElementById('status-indicator');
        this._lastUpdated = document.getElementById('last-updated');
        this._expandedGroups = new Set();
        this._manuallyCollapsed = new Set();
        this._expandedSteps = new Set();
        this._expandedValues = new Set();
        this._filter = '';
        this._statusFilter = 'active';
        this._viewMode = localStorage.getItem('rd-view-mode') || 'tree';  // 'tree' or 'flat'
        this._projectFilter = '';
        this._timeFilter = '7d';
        this._vizInstance = null;
        this._dotPanZoom = {};
        this._pendingDotRenders = {};  // dotId -> dotSource
        this._renderedDotCache = {};   // dotId -> rendered SVG outerHTML
        this._dotViewState = {};       // dotId -> { pan: {x,y}, zoom: number }
        this._collapsedSections = new Set();
        this._openedSections = new Set();
        this._expandedTasks = new Set();
        this._allTasksExpanded = false;
        this._activeOutcomeTab = 'summary';

        // Hash-based routing
        window.addEventListener('hashchange', () => {
            const hash = window.location.hash.slice(1);
            if (hash && hash !== this._currentSessionId) {
                this._currentSessionId = hash;
                this._showDetail(hash);
                this._startPolling();
            } else if (!hash && this._currentSessionId) {
                this._currentSessionId = null;
                this._showDiscovery();
                this._startPolling();
            }
        });

        // Pause polling when tab is hidden
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                this._stopPolling();
            } else {
                this._poll();
                this._startPolling();
            }
        });

        // Event delegation for group header clicks
        this._el.addEventListener('click', (e) => {
            const header = e.target.closest('.group-header[data-group]');
            if (header) {
                this.toggleGroup(header.dataset.group);
            }
        });

        const hash = window.location.hash.slice(1);
        if (hash) this._currentSessionId = hash;

        this._loadProjects();
        this._start();
    }

    async _loadProjects() {
        try {
            const params = this._timeFilter ? `?since=${this._timeFilter}` : '';
            const res = await fetch(`/api/projects${params}`);
            const data = await res.json();
            const select = document.getElementById('project-select');
            const current = select.value;
            // Keep first option ("All projects"), replace the rest
            while (select.options.length > 1) select.remove(1);
            for (const p of data.projects || []) {
                const opt = document.createElement('option');
                opt.value = p.slug;
                opt.textContent = `${p.short_name} (${p.count})`;
                select.appendChild(opt);
            }
            select.value = current; // preserve selection
        } catch { /* ignore */ }
    }

    async _start() {
        if (this._currentSessionId) {
            await this._showDetail(this._currentSessionId);
        } else {
            await this._showDiscovery();
        }
        this._startPolling();
    }

    _startPolling() {
        this._stopPolling();
        const interval = this._currentSessionId ? 10000 : 15000;
        this._pollInterval = setInterval(() => this._poll(), interval);
    }

    _stopPolling() {
        if (this._pollInterval) {
            clearInterval(this._pollInterval);
            this._pollInterval = null;
        }
    }

    async _poll() {
        const scrollY = window.scrollY;
        // Save DOT pan/zoom state before DOM rebuild
        this._saveDotViewStates();
        try {
            if (this._currentSessionId) {
                await this._showDetail(this._currentSessionId, true);
            } else {
                await this._showDiscovery();
            }
            this._statusDot.classList.add('active');
            this._lastUpdated.textContent = 'Updated ' + new Date().toLocaleTimeString();
            this._lastUpdated.style.color = '';
        } catch (e) {
            this._statusDot.classList.remove('active');
            this._lastUpdated.textContent = 'Connection lost \u2014 retrying...';
            this._lastUpdated.style.color = 'var(--yellow)';
        }
        window.scrollTo(0, scrollY);
    }

    async refresh() {
        await fetch('/api/refresh', { method: 'POST' });
        await this._poll();
    }

    setFilter(value) {
        this._filter = value.toLowerCase();
        this._showDiscovery();
    }

    setStatusFilter(status) {
        this._statusFilter = status;
        // Reset group expansion state when switching tabs
        this._expandedGroups.clear();
        this._manuallyCollapsed.clear();
        this._showDiscovery();
    }

    setProjectFilter(value) {
        this._projectFilter = value;
        // When selecting a specific project, show all statuses for it
        if (value) this._statusFilter = 'all';
        this._showDiscovery();
    }

    setTimeFilter(value) {
        this._timeFilter = value;
        this._loadProjects(); // refresh project counts for the new time range
        this._showDiscovery();
    }

    setViewMode(mode) {
        this._viewMode = mode;
        localStorage.setItem('rd-view-mode', mode);
        this._showDiscovery();
    }

    toggleGroup(project) {
        if (this._expandedGroups.has(project)) {
            this._expandedGroups.delete(project);
            this._manuallyCollapsed.add(project);
        } else {
            this._expandedGroups.add(project);
            this._manuallyCollapsed.delete(project);
        }
        this._showDiscovery();
    }

    // -- Discovery View ---------------------------------------------------

    async _showDiscovery() {
        let params = new URLSearchParams();
        if (this._projectFilter) params.set('project', this._projectFilter);
        if (this._timeFilter) params.set('since', this._timeFilter);
        const qs = params.toString() ? `?${params}` : '';

        let data;
        try {
            const res = await fetch(`/api/sessions${qs}`);
            data = await res.json();
        } catch {
            this._el.innerHTML = '<p>Failed to load sessions</p>';
            return;
        }

        const allSessions = data.sessions || [];
        this._lastSessionList = allSessions;  // Cache for child session lookup

        const isTree = this._viewMode === 'tree';
        const rootSessions = isTree
            ? allSessions.filter(s => !s.parent_id)
            : allSessions;

        // Count by status for tabs (root sessions only in tree mode)
        // Active = running + idle, Waiting = waiting, Stalled = stalled + failed + cancelled
        const countSource = isTree ? rootSessions : allSessions;
        const counts = { all: countSource.length, active: 0, waiting: 0, done: 0, stalled: 0 };
        for (const s of countSource) {
            if (s.status === 'running' || s.status === 'idle') counts.active++;
            else if (s.status === 'waiting') counts.waiting++;
            else if (s.status === 'done') counts.done++;
            else if (s.status === 'stalled' || s.status === 'failed' || s.status === 'cancelled') counts.stalled++;
        }

        // Apply status filter (against root sessions only in tree mode)
        let sessions;
        switch (this._statusFilter) {
            case 'active':
                sessions = rootSessions.filter(s => s.status === 'running' || s.status === 'idle');
                break;
            case 'waiting':
                sessions = rootSessions.filter(s => s.status === 'waiting');
                break;
            case 'done':
                sessions = rootSessions.filter(s => s.status === 'done');
                break;
            case 'stalled':
                sessions = rootSessions.filter(s => s.status === 'stalled' || s.status === 'failed' || s.status === 'cancelled');
                break;
            default:
                sessions = rootSessions;
        }

        // Group by project
        const byProject = {};
        for (const s of sessions) {
            const proj = s.project_slug || 'unknown';
            if (!byProject[proj]) byProject[proj] = [];
            byProject[proj].push(s);
        }

        // Determine which groups should be expanded
        // For Done/All tabs: expand all groups by default (user collapses manually)
        // For Active/Stalled/Waiting: only auto-expand groups with matching active sessions
        const expandAllByDefault = (this._statusFilter === 'done' || this._statusFilter === 'all');

        if (expandAllByDefault) {
            // Add all current groups unless user manually collapsed them
            for (const project of Object.keys(byProject)) {
                if (!this._manuallyCollapsed.has(project)) {
                    this._expandedGroups.add(project);
                }
            }
        } else {
            // Only auto-expand groups with running/waiting/stalled sessions
            for (const [project, projSessions] of Object.entries(byProject)) {
                if (!this._manuallyCollapsed.has(project) &&
                    projSessions.some(s => s.status === 'running' || s.status === 'waiting' || s.status === 'stalled')) {
                    this._expandedGroups.add(project);
                }
            }
        }

        let html = '';

        // Status filter tabs
        html += '<div class="filter-bar">';
        html += this._renderTab('active', `Active (${counts.active})`, this._statusFilter === 'active');
        html += this._renderTab('waiting', `Waiting (${counts.waiting})`, this._statusFilter === 'waiting');
        html += this._renderTab('stalled', `Stalled (${counts.stalled})`, this._statusFilter === 'stalled');
        html += this._renderTab('done', `Done (${counts.done})`, this._statusFilter === 'done');
        html += this._renderTab('all', `All (${counts.all})`, this._statusFilter === 'all');
        html += `<span class="view-toggle">`;
        html += `<button class="view-toggle-btn ${isTree ? 'active' : ''}" onclick="dashboard.setViewMode('tree')" title="Tree view">\u25e4 Tree</button>`;
        html += `<button class="view-toggle-btn ${!isTree ? 'active' : ''}" onclick="dashboard.setViewMode('flat')" title="Flat view">\u2630 Flat</button>`;
        html += `</span>`;
        html += '</div>';

        // Filter input
        html += `<input type="text" class="filter-input" placeholder="Filter by recipe, project, or session ID..."
                    value="${this._esc(this._filter)}" oninput="dashboard.setFilter(this.value)">`;

        if (sessions.length === 0) {
            html += `<p style="color:var(--text-muted);padding:40px;">No ${this._statusFilter} sessions.</p>`;
            this._el.innerHTML = html;
            return;
        }

        // Build child ID set for tree mode (used to skip/nest child sessions)
        const childIdSet = new Set();
        if (isTree) {
            for (const s of allSessions) {
                if (s.parent_id) childIdSet.add(s.session_id);
            }
        }

        // Flat list for active/waiting/stalled with few sessions
        const useFlat = (this._statusFilter === 'active' || this._statusFilter === 'waiting' || this._statusFilter === 'stalled') && sessions.length <= 20;

        if (useFlat) {
            const filtered = this._applyTextFilter(sessions);
            html += '<div class="session-list">';
            for (const s of filtered) {
                if (isTree && childIdSet.has(s.session_id)) continue;
                html += this._renderSessionRow(s, true);
                if (isTree) {
                    html += this._renderChildTree(s, allSessions, childIdSet);
                }
            }
            html += '</div>';
            this._el.innerHTML = html;
            return;
        }

        // Grouped sessions
        for (const [project, projSessions] of Object.entries(byProject)) {
            const filtered = this._applyTextFilter(projSessions, project);
            if (filtered.length === 0) continue;

            const shortProject = this._shortPath(projSessions[0]?.project_path, project);
            const isExpanded = this._expandedGroups.has(project);
            const chevron = isExpanded ? '\u25be' : '\u25b8';
            const runningCount = filtered.filter(s => s.status === 'running').length;
            const runningBadge = runningCount ? `, <span style="color:var(--green)">${runningCount} running</span>` : '';

            html += `
            <div class="group-header" data-group="${project.replace(/"/g, '&quot;')}">
                <span class="chevron">${chevron}</span>
                <span class="group-name">${this._esc(shortProject)}</span>
                <span class="group-meta">${filtered.length} sessions${runningBadge}</span>
            </div>`;

            if (isExpanded) {
                html += '<div class="session-list">';
                for (const s of filtered) {
                    if (isTree && childIdSet.has(s.session_id)) continue;
                    html += this._renderSessionRow(s);
                    if (isTree) {
                        html += this._renderChildTree(s, allSessions, childIdSet);
                    }
                }
                html += '</div>';
            }
        }

        this._el.innerHTML = html;
    }

    _applyTextFilter(sessions, project) {
        if (!this._filter) return sessions;
        return sessions.filter(s =>
            s.session_id.toLowerCase().includes(this._filter) ||
            s.recipe_name.toLowerCase().includes(this._filter) ||
            (s.plan_path || '').toLowerCase().includes(this._filter) ||
            (s.project_slug || '').toLowerCase().includes(this._filter) ||
            (project || '').toLowerCase().includes(this._filter));
    }

    _renderSessionRow(s, showProject = false, isChild = false) {
        const elapsed = this._timeAgo(s.started);
        const stepsDone = (s.completed_steps || []).length;
        const stepsTotal = s.total_steps || stepsDone;
        const pct = stepsTotal > 0 ? Math.round((stepsDone / stepsTotal) * 100) : 0;
        const planFile = s.plan_path ? s.plan_path.split('/').pop() : '';
        const shortProject = this._shortPath(s.project_path, s.project_slug);
        const sessionIdShort = s.session_id.slice(0, 8);

        const secondLine = showProject && shortProject
            ? `<span class="row-subtitle">${this._esc(shortProject)}${planFile ? ' / ' + this._esc(planFile) : ''}</span>`
            : (planFile ? `<span class="row-subtitle">${this._esc(planFile)}</span>` : '');

        return `
        <div class="session-row ${showProject ? 'with-subtitle' : ''} ${isChild ? 'child-row' : ''}" tabindex="0" role="button"
             title="Session: ${s.session_id}"
             onclick="dashboard.navigateTo('${s.session_id}')"
             onkeydown="event.key==='Enter'&&dashboard.navigateTo('${s.session_id}')">
            <span class="status ${s.status}">${s.status}</span>
            <span class="recipe-info">
                <span class="recipe-name">${this._esc(s.recipe_name)}</span>
                ${secondLine}
            </span>
            <span class="progress-cell">
                <span class="mini-progress"><span class="mini-progress-fill" style="width:${pct}%"></span></span>
                <span class="progress-text">${stepsDone}/${stepsTotal}</span>
            </span>
            <span class="time">
                ${elapsed}
                <span class="session-id-hint">${sessionIdShort}</span>
            </span>
        </div>`;
    }

    _renderTab(value, label, isActive) {
        const cls = isActive ? 'filter-tab active' : 'filter-tab';
        return `<button class="${cls}" onclick="dashboard.setStatusFilter('${value}')">${label}</button>`;
    }

    /**
     * Toggle a tree node open/closed in the discovery view and re-render.
     * Simpler than toggleSection — no DOM manipulation needed, always re-renders.
     */
    toggleTreeNode(treeKey) {
        if (this._openedSections.has(treeKey)) {
            this._openedSections.delete(treeKey);
            this._collapsedSections.add(treeKey);
        } else {
            this._collapsedSections.delete(treeKey);
            this._openedSections.add(treeKey);
        }
        this._showDiscovery();
    }

    /**
     * Render child sessions as an indented tree under a parent in the discovery list.
     * Auto-expands if parent is active, collapses if parent is terminal.
     * @param {object} parentSession  - the parent session object
     * @param {Array}  allSessions    - full unfiltered session list for lookup
     * @param {Set}    childIdSet     - set of all session IDs that have a parent
     * @param {number} depth          - current nesting depth (0 = top-level children)
     */
    _renderChildTree(parentSession, allSessions, childIdSet, depth = 0) {
        const children = (parentSession.child_session_ids || [])
            .map(id => allSessions.find(s => s.session_id === id))
            .filter(Boolean);
        if (children.length === 0) return '';

        const isActive = ['running', 'idle', 'waiting'].includes(parentSession.status);
        const treeKey = `tree-${parentSession.session_id}`;

        // Auto-expand active parents, collapse terminal ones. User override sticks.
        let isExpanded;
        if (this._openedSections.has(treeKey)) {
            isExpanded = true;
        } else if (this._collapsedSections.has(treeKey)) {
            isExpanded = false;
        } else {
            isExpanded = isActive;
        }

        if (!isExpanded) {
            const summary = `${children.length} sub-recipe${children.length > 1 ? 's' : ''}`;
            return `<div class="child-tree-collapsed" style="margin-left:${24 + depth * 20}px;"
                         onclick="dashboard.toggleTreeNode('${treeKey}')"
                         role="button" tabindex="0"
                         onkeydown="event.key==='Enter'&&dashboard.toggleTreeNode('${treeKey}')">
                <span class="chevron">\u25b8</span> ${summary}
            </div>`;
        }

        let html = '';
        for (const child of children) {
            const stepsDone = (child.completed_steps || []).length;
            const stepsTotal = child.total_steps || stepsDone;
            const pct = stepsTotal > 0 ? Math.round((stepsDone / stepsTotal) * 100) : 0;
            const elapsed = this._timeAgo(child.started);

            const sessionIdShort = child.session_id.slice(0, 8);
            html += `<div class="session-row child-row" style="margin-left:${24 + depth * 20}px;"
                          tabindex="0" role="button"
                          title="Session: ${child.session_id}"
                          onclick="dashboard.navigateTo('${child.session_id}')"
                          onkeydown="event.key==='Enter'&&dashboard.navigateTo('${child.session_id}')">
                <span class="status ${child.status}">${child.status}</span>
                <span class="recipe-info">
                    <span class="recipe-name">${this._esc(child.recipe_name)}</span>
                </span>
                <span class="progress-cell">
                    <span class="mini-progress"><span class="mini-progress-fill" style="width:${pct}%"></span></span>
                    <span class="progress-text">${stepsDone}/${stepsTotal}</span>
                </span>
                <span class="time">
                    ${elapsed}
                    <span class="session-id-hint">${sessionIdShort}</span>
                </span>
            </div>`;
            // Recurse for grandchildren
            html += this._renderChildTree(child, allSessions, childIdSet, depth + 1);
        }
        return html;
    }

    // -- Detail View ------------------------------------------------------

    async _showDetail(sessionId, isPoll = false) {
        let session, tasks;
        try {
            const [sRes, tRes] = await Promise.all([
                fetch(`/api/session/${sessionId}`),
                fetch(`/api/session/${sessionId}/tasks`),
            ]);
            session = await sRes.json();
            tasks = await tRes.json();
        } catch {
            this._el.innerHTML = '<p>Failed to load session</p>';
            return;
        }

        if (session.error) {
            this._el.innerHTML = `<p>${this._esc(session.error)}</p>`;
            return;
        }

        // On poll refresh, skip DOM rebuild if data hasn't changed
        // This prevents DOT graph zoom/pan flash
        if (isPoll) {
            const dataHash = JSON.stringify({
                status: session.status,
                completed_steps: session.completed_steps,
                progress: session.progress,
                tasks_done: tasks.done,
            });
            if (this._lastDetailHash === dataHash) {
                // Data unchanged -- just update the timestamp, don't touch DOM
                return;
            }
            this._lastDetailHash = dataHash;
        } else {
            this._lastDetailHash = null;
        }

        const status = session.status;
        const planFile = session.plan_path ? session.plan_path.split('/').pop() : 'No plan file';
        const elapsed = this._timeAgo(session.started);
        const absTime = session.started ? new Date(session.started).toLocaleString() : '';
        const sessionIdShort = session.session_id.slice(0, 16);
        const parentId = session.parent_id || '';

        let html = `
        <div class="detail-header">
            <span class="back-link" tabindex="0" role="button"
                  onclick="dashboard.navigateBack()"
                  onkeydown="event.key==='Enter'&&dashboard.navigateBack()">&larr; All sessions</span>
            <h2>${this._esc(session.recipe_name)}</h2>
            <div class="meta">
                <span class="status ${status}" style="margin-right:8px;">${status.toUpperCase()}</span>
                ${this._esc(planFile)} &middot; Started ${elapsed}
                <span title="${absTime}" style="cursor:help;"> (${absTime})</span>
                &middot; ${session.recipe_version || ''}
            </div>
            ${session.recipe_description ? `<div class="recipe-description">${this._esc(session.recipe_description)}</div>` : ''}
            ${session.is_staged && session.current_stage ? `<div class="stage-progress">Stage: <span class="stage-name">${this._esc(session.current_stage)}</span> &middot; ${(session.completed_stages || []).length} stages completed</div>` : ''}
            <div class="session-ids">
                <span class="session-id-label">Recipe Session ID:</span>
                <span class="session-id-value" title="${session.session_id}">${sessionIdShort}</span>
                ${parentId ? `<span class="session-id-label" style="margin-left:12px;">Parent:</span>
                <span class="session-id-value clickable" title="${parentId}" onclick="dashboard.navigateTo('${parentId}')">${parentId.slice(0, 16)}</span>` : ''}
                ${session.session_dir ? `<br><span class="session-id-label">Session Path:</span>
                <span class="session-id-value session-path" title="${this._esc(session.session_dir)}">${this._esc(session.session_dir.replace(/\/Users\/[^/]+\//, '~/'))}</span>` : ''}
            </div>
        </div>`;

        // Status-specific banners
        if (status === 'waiting') {
            html += this._renderApprovalBanner(session);
        } else if (status === 'cancelled') {
            html += this._renderStatusBanner(session, 'cancelled');
        } else if (status === 'failed') {
            html += this._renderStatusBanner(session, 'failed');
        }

        // Build section content
        const stepsHtml = this._renderRecipeSteps(session);
        const tasksHtml = this._buildTasksSection(session, tasks);
        const contextHtml = this._renderContextSummary(session);
        const outcomeHtml = this._renderOutcomeTabs(session);
        const completedTasksHtml = this._renderCompletedTasks(session);
        const timelineHtml = this._renderApprovalTimeline(session);
        // Status-adaptive section layout with collapse defaults
        const isTerminal = (status === 'done' || status === 'cancelled' || status === 'failed');
        const isWaiting = (status === 'waiting');
        // For terminal states, only collapse steps/context if there's richer content to show above them
        const hasRichContent = !!(outcomeHtml || completedTasksHtml);

        // Approval timeline for waiting status (directly after banner)
        if (isWaiting && timelineHtml) {
            html += this._renderCollapsibleSection(
                'timeline', 'Approval History', timelineHtml,
                false  // expanded for waiting
            );
        }

        // Outcome tabs (for done/terminal, before tasks)
        if (outcomeHtml) {
            html += this._renderCollapsibleSection(
                'outcome', 'Outcome', outcomeHtml,
                isWaiting  // collapsed for waiting, expanded otherwise
            );
        }

        // Completed tasks accordion
        if (completedTasksHtml) {
            html += this._renderCollapsibleSection(
                'completed-tasks', 'Implementation Reports', completedTasksHtml,
                isWaiting  // collapsed for waiting, expanded otherwise
            );
        }

        // Recipe Steps section
        // Only collapse if there's richer content above (outcome tabs / completed tasks)
        html += this._renderCollapsibleSection(
            'steps', 'Recipe Steps', stepsHtml,
            (isTerminal && hasRichContent) || isWaiting
        );

        // Plan Tasks section
        if (tasksHtml) {
            html += this._renderCollapsibleSection(
                'tasks', 'Plan Tasks', tasksHtml,
                isWaiting  // collapsed for waiting, expanded for everything else
            );
        }

        // Approval timeline for non-waiting states (as collapsed section)
        if (!isWaiting && timelineHtml) {
            html += this._renderCollapsibleSection(
                'timeline', 'Approval History', timelineHtml,
                true  // collapsed by default
            );
        }

        // Context section -- expanded by default (useful reference data)
        if (contextHtml) {
            html += this._renderCollapsibleSection(
                'context', 'Context', contextHtml,
                isWaiting  // only collapsed when waiting (everything else collapses there)
            );
        }

        this._el.innerHTML = html;
    }

    /**
     * Build the tasks section content (progress bar + table + commits).
     * Returns empty string if no tasks.
     */
    _buildTasksSection(session, tasks) {
        if (tasks.tasks && tasks.tasks.length > 0) {
            const done = tasks.done || 0;
            const total = tasks.total || tasks.tasks.length;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;
            const barColor = pct >= 60 ? 'var(--green)' : pct >= 20 ? 'var(--yellow)' : 'var(--border)';

            let html = `
            <div class="progress-bar-container">
                <div class="progress-bar-fill" style="width:${pct}%;background:${barColor}"></div>
                <span class="progress-bar-label">${done} / ${total} tasks (${pct}%)</span>
            </div>`;
            html += this._renderTaskTable(tasks.tasks);
            if (tasks.recent_commits && tasks.recent_commits.length > 0) {
                html += this._renderRecentCommits(tasks.recent_commits);
            }
            return html;
        }
        if (tasks.error) {
            return `<p style="color:var(--text-muted);margin-top:8px;">${this._esc(tasks.error)}</p>`;
        }
        return '';
    }

    /**
     * Render a collapsible section with chevron toggle.
     * Respects user's manual toggle via _collapsedSections/_openedSections.
     */
    _renderCollapsibleSection(id, label, contentHtml, defaultCollapsed = false) {
        if (!contentHtml) return '';

        // Determine collapsed state: user override > default
        let isCollapsed;
        if (this._openedSections.has(id)) {
            isCollapsed = false;
        } else if (this._collapsedSections.has(id)) {
            isCollapsed = true;
        } else {
            isCollapsed = defaultCollapsed;
        }

        const chevron = isCollapsed ? '\u25b8' : '\u25be';
        return `
        <div class="collapsible-section">
            <div class="collapsible-header" onclick="dashboard.toggleSection('${id}')">
                <span class="collapsible-chevron">${chevron}</span>
                <h3 class="section-label" style="margin:0;cursor:pointer;">${label}</h3>
            </div>
            <div class="collapsible-body" style="display:${isCollapsed ? 'none' : 'block'}">
                ${contentHtml}
            </div>
        </div>`;
    }

    /**
     * Toggle a collapsible section open/closed.
     */
    toggleSection(sectionId) {
        if (this._openedSections.has(sectionId)) {
            // User is closing a section they previously opened
            this._openedSections.delete(sectionId);
            this._collapsedSections.add(sectionId);
        } else if (this._collapsedSections.has(sectionId)) {
            // User is opening a section they previously closed
            this._collapsedSections.delete(sectionId);
            this._openedSections.add(sectionId);
        } else {
            // First toggle — check current DOM state
            const header = event.currentTarget;
            const body = header?.nextElementSibling;
            if (body && body.style.display === 'none') {
                this._openedSections.add(sectionId);
            } else {
                this._collapsedSections.add(sectionId);
            }
        }
        // Toggle DOM directly for instant feedback (no re-render)
        const section = event.currentTarget.closest('.collapsible-section');
        if (section) {
            const body = section.querySelector('.collapsible-body');
            const chevron = section.querySelector('.collapsible-chevron');
            if (body && chevron) {
                const nowHidden = body.style.display !== 'none';
                body.style.display = nowHidden ? 'none' : 'block';
                chevron.textContent = nowHidden ? '\u25b8' : '\u25be';
            }
        }
    }

    /**
     * Render the approval banner for sessions in "waiting" state.
     */
    _renderApprovalBanner(session) {
        if (!session.pending_approval_stage) return '';
        return `
        <div class="approval-banner">
            <div class="approval-banner-title">\u23f8 Approval Required</div>
            <div class="approval-banner-stage">Stage: ${this._esc(session.pending_approval_stage)}</div>
            ${session.pending_approval_prompt ?
                `<div class="approval-banner-prompt">${this._esc(session.pending_approval_prompt)}</div>` : ''}
        </div>`;
    }

    /**
     * Render a status banner for cancelled/failed sessions.
     */
    _renderStatusBanner(session, type) {
        if (type === 'cancelled') {
            return `
            <div class="status-banner cancelled">
                <div class="status-banner-title">\u2014 Recipe Cancelled</div>
                <div class="status-banner-detail">Cancellation status: ${this._esc(session.cancellation_status || 'cancelled')}</div>
            </div>`;
        }
        if (type === 'failed') {
            return `
            <div class="status-banner failed">
                <div class="status-banner-title">\u2717 Recipe Failed</div>
                <div class="status-banner-detail">The recipe stopped unexpectedly during execution.</div>
            </div>`;
        }
        return '';
    }

    _renderRecipeSteps(session) {
        const steps = session.recipe_steps || [];
        if (steps.length === 0) return '';

        let activeId = null;
        for (const step of steps) {
            if (!step.completed && !step.skipped) { activeId = step.id; break; }
        }

        // Build child session lookup for inline sub-recipe display
        const childIds = session.child_session_ids || [];
        const allSessions = this._lastSessionList || [];
        const byId = {};
        for (const s of allSessions) byId[s.session_id] = s;
        const childSessions = childIds
            .map(id => byId[id])
            .filter(Boolean)
            .sort((a, b) => (a.started || '').localeCompare(b.started || ''));
        const recipeStepQueue = [...childSessions];

        let html = '<div class="steps-detail">';
        for (let i = 0; i < steps.length; i++) {
            const step = steps[i];
            let cls = '', icon = '\u2013';
            if (step.completed) { cls = 'completed'; icon = '\u2713'; }
            else if (step.skipped) { cls = 'skipped'; icon = '\u21b7'; }
            else if (step.id === activeId) { cls = 'active'; icon = '\u2192'; }

            const isRecipeStep = (step.type === 'recipe');
            const childSession = isRecipeStep ? recipeStepQueue.shift() : null;

            const typeLabel = step.type || '';
            const outputKey = step.output_key ? `\u2192 ${step.output_key}` : '';
            const hasDetail = step.completed && (step.output_value || step.description);
            const hasVars = step.resolved_variables && Object.keys(step.resolved_variables).length > 0;
            const hasCondition = !!step.condition;
            const showPanel = hasDetail || hasVars || (step.skipped && hasCondition) || isRecipeStep;
            const panelId = `step-panel-${i}`;

            html += `<div class="step-row ${cls} ${showPanel ? 'clickable' : ''}"
                          ${showPanel ? `role="button" tabindex="0" onclick="dashboard.toggleStepPanel('${panelId}')" onkeydown="event.key==='Enter'&&dashboard.toggleStepPanel('${panelId}')"` : ''}>`;
            html += `<span class="step-icon">${icon}</span>`;
            html += `<div class="step-info">`;
            html += `<span class="step-name">${this._esc(step.id)}</span>`;
            html += `<span class="step-type">${this._esc(typeLabel)}</span>`;
            if (childSession) {
                const cStatus = childSession.status || 'unknown';
                const cStepsDone = (childSession.completed_steps || []).length;
                const cStepsTotal = childSession.total_steps || cStepsDone;
                html += `<span class="step-sub-recipe-badge">`;
                html += `<span class="status ${cStatus}">${cStatus}</span>`;
                html += ` ${this._esc(childSession.recipe_name)} ${cStepsDone}/${cStepsTotal}`;
                html += `</span>`;
            }
            if (outputKey && !childSession) html += `<span class="step-output-key">${this._esc(outputKey)}</span>`;
            if (step.skipped) html += `<span class="step-skipped-badge">skipped</span>`;
            else if (step.condition && !step.completed) html += `<span class="step-condition-badge">conditional</span>`;
            if (showPanel) {
                const isStepOpen = this._expandedSteps.has(panelId);
                html += `<span class="step-expand-hint">${isStepOpen ? '\u25be' : '\u25b8'}</span>`;
            }
            html += `</div></div>`;

            // Collapsible detail panel
            if (showPanel) {
                const isStepOpen = this._expandedSteps.has(panelId);
                html += `<div id="${panelId}" class="step-detail-panel" style="display:${isStepOpen ? 'block' : 'none'};">`;
                if (childSession) {
                    html += this._renderInlineSubRecipe(childSession, 0);
                }
                if (step.skipped && step.condition) {
                    const highlightedCond = this._highlightTemplateVars(step.condition);
                    html += `<div class="step-detail-row"><span class="step-detail-label">Skipped — Condition was false</span><div class="step-detail-value"><code class="condition-code">${highlightedCond}</code></div></div>`;
                } else if (step.condition) {
                    const highlightedCond = this._highlightTemplateVars(step.condition);
                    html += `<div class="step-detail-row"><span class="step-detail-label">Condition</span><div class="step-detail-value"><code class="condition-code">${highlightedCond}</code></div></div>`;
                }
                if (step.description) {
                    const highlighted = this._highlightTemplateVars(step.description);
                    html += `<div class="step-detail-row"><span class="step-detail-label">Description</span><div class="step-detail-value"><pre class="step-output-pre">${highlighted}</pre></div></div>`;
                }
                // Show resolved template variables
                if (hasVars) {
                    html += `<div class="step-detail-row"><span class="step-detail-label">Resolved Variables</span><div class="resolved-vars">`;
                    for (const [varName, varVal] of Object.entries(step.resolved_variables)) {
                        const stableId = `var-${i}-${varName}`;
                        html += `<div class="resolved-var-row">`;
                        html += `<span class="var-name">{{${this._esc(varName)}}}</span>`;
                        html += `<span class="var-arrow">\u2192</span>`;
                        html += `<span class="var-value">${this._renderExpandableValue(varVal, stableId)}</span>`;
                        html += `</div>`;
                    }
                    html += `</div></div>`;
                }
                if (step.output_value) {
                    const outputId = `step-out-${i}`;
                    html += `<div class="step-detail-row"><span class="step-detail-label">Output (${this._esc(step.output_key)})</span><div class="step-detail-value">${this._renderExpandableValue(step.output_value, outputId)}</div></div>`;
                }
                html += `</div>`;
            }
        }
        html += '</div>';
        return html;
    }

    _renderInlineSubRecipe(childSession, depth) {
        const allSessions = this._lastSessionList || [];
        const byId = {};
        for (const s of allSessions) byId[s.session_id] = s;

        const cStatus = childSession.status || 'unknown';
        const cSteps = childSession.recipe_steps || [];
        const completedSet = new Set(childSession.completed_steps || []);
        const cStepsDone = completedSet.size;
        const cStepsTotal = childSession.total_steps || cStepsDone;
        const elapsed = this._timeAgo(childSession.started);
        const grandchildIds = childSession.child_session_ids || [];
        const grandchildren = grandchildIds
            .map(id => byId[id]).filter(Boolean)
            .sort((a, b) => (a.started || '').localeCompare(b.started || ''));
        const gcQueue = [...grandchildren];

        let html = `<div class="inline-sub-recipe" style="margin-left:${depth * 16}px;">`;
        html += `<div class="inline-sub-recipe-header"
                      tabindex="0" role="button"
                      onclick="event.stopPropagation(); dashboard.navigateTo('${childSession.session_id}')"
                      onkeydown="event.key==='Enter'&&(event.stopPropagation(), dashboard.navigateTo('${childSession.session_id}'))">`;
        html += `<span class="status ${cStatus}">${cStatus}</span>`;
        html += `<span class="inline-sub-recipe-name">${this._esc(childSession.recipe_name)}</span>`;
        html += `<span class="progress-cell">`;
        html += `<span class="mini-progress"><span class="mini-progress-fill" style="width:${cStepsTotal > 0 ? Math.round((cStepsDone / cStepsTotal) * 100) : 0}%"></span></span>`;
        html += `<span class="progress-text">${cStepsDone}/${cStepsTotal}</span>`;
        html += `</span>`;
        html += `<span class="time">${elapsed}</span>`;
        html += `</div>`;

        if (cSteps.length > 0) {
            html += `<div class="inline-sub-recipe-steps">`;
            for (const cs of cSteps) {
                const done = completedSet.has(cs.id);
                const isSubRecipe = (cs.type === 'recipe');
                const gcSession = isSubRecipe ? gcQueue.shift() : null;
                const sIcon = done ? '\u2713' : (cs.skipped ? '\u21b7' : '\u2013');
                const sCls = done ? 'completed' : (cs.skipped ? 'skipped' : '');
                html += `<div class="inline-step ${sCls}">`;
                html += `<span class="step-icon">${sIcon}</span>`;
                html += `<span class="step-name">${this._esc(cs.id)}</span>`;
                html += `<span class="step-type">${this._esc(cs.type || '')}</span>`;
                if (gcSession) {
                    const gcStatus = gcSession.status || 'unknown';
                    html += ` <span class="step-sub-recipe-badge"><span class="status ${gcStatus}">${gcStatus}</span> ${this._esc(gcSession.recipe_name)}</span>`;
                }
                html += `</div>`;
                if (gcSession) {
                    html += this._renderInlineSubRecipe(gcSession, depth + 1);
                }
            }
            html += `</div>`;
        }
        html += `</div>`;
        return html;
    }

    _highlightTemplateVars(text) {
        // Escape HTML first, then highlight {{var}} patterns
        const escaped = this._esc(text);
        return escaped.replace(/\{\{(\w+)\}\}/g, '<span class="template-var">{{$1}}</span>');
    }

    toggleStepPanel(panelId) {
        if (this._expandedSteps.has(panelId)) {
            this._expandedSteps.delete(panelId);
        } else {
            this._expandedSteps.add(panelId);
        }
        const panel = document.getElementById(panelId);
        if (!panel) return;
        const isOpen = this._expandedSteps.has(panelId);
        panel.style.display = isOpen ? 'block' : 'none';
        const row = panel.previousElementSibling;
        if (row) {
            const hint = row.querySelector('.step-expand-hint');
            if (hint) hint.textContent = isOpen ? '\u25be' : '\u25b8';
        }
        // When opening a panel, trigger any pending DOT renders inside it
        if (isOpen && this._pendingDotRenders) {
            setTimeout(() => {
                panel.querySelectorAll('.dot-graph-viewport').forEach(vp => {
                    if (vp.querySelector('svg.dot-rendered')) return;
                    const dotId = vp.id.replace('-viewport', '');
                    const source = this._pendingDotRenders[dotId];
                    if (source) this._renderDotGraph(dotId, source);
                });
            }, 100);
        }
    }

    toggleExpandable(id) {
        if (this._expandedValues.has(id)) {
            this._expandedValues.delete(id);
        } else {
            this._expandedValues.add(id);
        }
        const short = document.getElementById(id + '-short');
        const full = document.getElementById(id + '-full');
        if (!short || !full) return;
        const isOpen = this._expandedValues.has(id);
        short.style.display = isOpen ? 'none' : '';
        full.style.display = isOpen ? 'block' : 'none';
    }

    _renderExpandableValue(value, stableId) {
        const maxLen = 150;
        const valStr = typeof value === 'string' ? value : JSON.stringify(value);
        const id = stableId || ('exp-' + Math.random().toString(36).slice(2, 8));
        const copyId = id + '-copy';
        const copyBtn = `<button class="copy-btn" title="Copy to clipboard" onclick="event.stopPropagation();dashboard.copyFromElement('${copyId}', this)">Copy</button>`;
        const copyStore = `<span id="${copyId}" class="copy-store">${this._esc(valStr)}</span>`;
        const contentType = this._detectContentType(valStr);
        const hasPreview = (contentType === 'dot' && typeof Viz !== 'undefined') || contentType === 'markdown' || contentType === 'json';

        // Renderable content types: show preview by default, "View Source" toggle
        if (hasPreview) {
            const showingSource = this._expandedValues.has(id);
            let preview = '';
            let toolbarExtra = '';

            if (contentType === 'dot' && typeof Viz !== 'undefined') {
                const dotId = 'dot-' + id.replace(/[^a-z0-9]/g, '');
                const cached = this._renderedDotCache[dotId];
                toolbarExtra = `<button class="dot-btn" onclick="event.stopPropagation();dashboard.dotFit('${dotId}')">Fit</button><button class="dot-btn" onclick="event.stopPropagation();dashboard.dotZoomIn('${dotId}')">+</button><button class="dot-btn" onclick="event.stopPropagation();dashboard.dotZoomOut('${dotId}')">-</button>`;
                if (cached) {
                    // Restore from cache instantly — no loader flash
                    preview = `<div id="${dotId}" class="dot-graph-container"><div class="dot-graph-viewport" id="${dotId}-viewport">${cached}</div></div>`;
                    // Re-init svgPanZoom after DOM insert
                    setTimeout(() => {
                        const vp = document.getElementById(dotId + "-viewport");
                        const svg = vp?.querySelector("svg.dot-rendered");
                        if (svg) {
                            try {
                                const pz = svgPanZoom(svg, { zoomEnabled: true, controlIconsEnabled: false, dblClickZoomEnabled: false, fit: false, center: false, minZoom: 0.1, maxZoom: 20, zoomScaleSensitivity: 0.3 });
                                this._dotPanZoom[dotId] = pz;
                                setTimeout(() => { pz.resize(); this._restoreDotViewState(dotId, pz); }, 50);
                            } catch {} 
                        }
                    }, 50);
                } else {
                    // First render — show loader, schedule async render
                    this._scheduleDotRender(dotId, valStr);
                    preview = `<div id="${dotId}" class="dot-graph-container"><div class="dot-graph-viewport" id="${dotId}-viewport"><div class="dot-graph-loader"><svg viewBox="0 0 400 280" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Loading graph" fill="none"><line class="dgl-rank" x1="50" y1="102" x2="350" y2="102"/><line class="dgl-rank" x1="50" y1="200" x2="350" y2="200"/><rect class="dgl-scan" x="40" y="25" width="320" height="1.5" rx="0.75"/><path class="dgl-edge dgl-et" d="M200,55 C175,85 125,118 90,150" pathLength="1"/><path class="dgl-edge dgl-et" d="M200,55 L200,150" pathLength="1"/><path class="dgl-edge dgl-et" d="M200,55 C225,85 275,118 310,150" pathLength="1"/><path class="dgl-edge dgl-em" d="M90,150 C85,182 68,218 60,250" pathLength="1"/><path class="dgl-edge dgl-em" d="M90,150 C108,188 148,222 175,250" pathLength="1"/><path class="dgl-edge dgl-em" d="M200,150 C195,188 182,222 175,250" pathLength="1"/><path class="dgl-edge dgl-em" d="M310,150 C315,182 326,218 330,250" pathLength="1"/><path class="dgl-edge dgl-ex" d="M60,250 C92,274 143,274 175,250" pathLength="1"/><circle class="dgl-ring" cx="200" cy="55" r="22"/><circle class="dgl-node dgl-n0" cx="200" cy="55" r="14"/><circle class="dgl-node dgl-n1a" cx="90" cy="150" r="11"/><circle class="dgl-node dgl-n1b" cx="200" cy="150" r="11"/><circle class="dgl-node dgl-n1c" cx="310" cy="150" r="11"/><circle class="dgl-node dgl-n2a" cx="60" cy="250" r="9"/><circle class="dgl-node dgl-n2b" cx="175" cy="250" r="9"/><circle class="dgl-node dgl-n2c" cx="330" cy="250" r="9"/></svg><span>Rendering graph...</span></div></div></div>`;
                }
            } else if (contentType === 'markdown') {
                preview = `<div class="typed-content md-content">${this._renderSimpleMarkdown(valStr)}</div>`;
            } else if (contentType === 'json') {
                try {
                    const formatted = JSON.stringify(JSON.parse(valStr), null, 2);
                    preview = `<pre class="typed-content json-content"><code>${this._esc(formatted)}</code></pre>`;
                } catch {
                    preview = `<pre class="typed-content"><code>${this._esc(valStr)}</code></pre>`;
                }
            }

            return `<div class="step-output-value preview-wrapper">
                <div class="expanded-content-toolbar">
                    <span class="content-type-badge">${contentType.toUpperCase()}</span>
                    ${toolbarExtra}
                    <button class="expand-btn toolbar-btn" onclick="event.stopPropagation();dashboard.togglePreviewSource('${id}')">${showingSource ? 'View Preview' : 'View Source'}</button>
                    ${copyBtn}
                </div>
                <div id="${id}-preview" style="display:${showingSource ? 'none' : 'block'}">${preview}</div>
                <div id="${id}-source" class="source-view" style="display:${showingSource ? 'block' : 'none'}"><pre class="typed-content"><code>${contentType === 'dot' ? this._highlightDot(valStr) : this._esc(valStr)}</code></pre></div>
                ${copyStore}
            </div>`;
        }

        // Short plain text: no expand needed
        if (valStr.length <= maxLen) {
            return `<div class="step-output-value">${this._esc(valStr)}${copyBtn}${copyStore}</div>`;
        }

        // Long plain text: show more / show less
        const isExpanded = this._expandedValues.has(id);
        return `<div class="step-output-value expandable"><span id="${id}-short" style="display:${isExpanded ? 'none' : ''}">${this._esc(valStr.slice(0, maxLen))}... <button class="expand-btn" onclick="event.stopPropagation();dashboard.toggleExpandable('${id}')">Show more</button></span><div id="${id}-full" class="expanded-content-wrapper" style="display:${isExpanded ? 'block' : 'none'}"><div class="expanded-content-toolbar"><button class="expand-btn toolbar-btn" onclick="event.stopPropagation();dashboard.toggleExpandable('${id}')">Show less</button>${copyBtn}</div><div class="expanded-content-body">${this._esc(valStr)}</div></div>${copyStore}</div>`;
    }

    togglePreviewSource(id) {
        if (this._expandedValues.has(id)) {
            this._expandedValues.delete(id);
        } else {
            this._expandedValues.add(id);
        }
        const previewDiv = document.getElementById(id + '-preview');
        const sourceDiv = document.getElementById(id + '-source');
        if (!previewDiv || !sourceDiv) return;
        const showingSource = this._expandedValues.has(id);
        previewDiv.style.display = showingSource ? 'none' : 'block';
        sourceDiv.style.display = showingSource ? 'block' : 'none';
        // Update button text
        const wrapper = previewDiv.parentElement;
        if (wrapper) {
            const btn = wrapper.querySelector('.toolbar-btn');
            if (btn) btn.textContent = showingSource ? 'View Preview' : 'View Source';
        }
    }

    _detectContentType(text) {
        const trimmed = text.trim();
        if (/^(strict\s+)?(di)?graph\s/i.test(trimmed) || /^(strict\s+)?(di)?graph\s*\{/i.test(trimmed)) return 'dot';
        if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
            try { JSON.parse(trimmed); return 'json'; } catch { /* not json */ }
        }
        if (/^#{1,6}\s/m.test(trimmed) || /^\*\*[^*]+\*\*/m.test(trimmed) || /^```/m.test(trimmed)) return 'markdown';
        return 'text';
    }

    _renderTypedContent(text, type) {
        switch (type) {
            case 'dot':
                // DOT rendering is handled in _renderExpandableValue for the preview/source toggle
                return `<pre class="typed-content dot-content"><code>${this._highlightDot(text)}</code></pre>`;
            case 'json':
                try {
                    const formatted = JSON.stringify(JSON.parse(text), null, 2);
                    return `<pre class="typed-content json-content"><code>${this._esc(formatted)}</code></pre>`;
                } catch {
                    return `<pre class="typed-content"><code>${this._esc(text)}</code></pre>`;
                }
            case 'markdown':
                return `<div class="typed-content md-content">${this._renderSimpleMarkdown(text)}</div>`;
            default:
                return this._esc(text);
        }
    }

    _highlightDot(text) {
        // Tokenize first, then escape each token and wrap with spans.
        // This avoids the double-escape problem of escaping-then-inserting-HTML.
        const keywords = new Set(['digraph','graph','subgraph','strict','node','edge','rankdir','label','shape','style','color','fillcolor','fontname','fontsize','fontcolor']);
        // Split into tokens: strings, comments, arrows, words, and other chars
        const tokens = text.match(/"[^"]*"|\/\/[^\n]*|->|--|\w+|[^\s]|\s+/g) || [];
        return tokens.map(t => {
            if (t.startsWith('"')) return `<span class="dot-str">${this._esc(t)}</span>`;
            if (t.startsWith('//')) return `<span class="dot-cmt">${this._esc(t)}</span>`;
            if (t === '->' || t === '--') return `<span class="dot-arrow">${this._esc(t)}</span>`;
            if (keywords.has(t)) return `<span class="dot-kw">${this._esc(t)}</span>`;
            return this._esc(t);
        }).join('');
    }

    _saveDotViewStates() {
        // Capture current pan/zoom from all active svgPanZoom instances
        for (const [dotId, pz] of Object.entries(this._dotPanZoom || {})) {
            try {
                const pan = pz.getPan();
                const zoom = pz.getZoom();
                if (pan && zoom) {
                    this._dotViewState[dotId] = { pan, zoom };
                }
            } catch { /* pz may be destroyed */ }
        }
    }

    _restoreDotViewState(dotId, pz) {
        // Restore saved pan/zoom state instead of fitting to center
        const saved = this._dotViewState[dotId];
        if (saved) {
            try {
                pz.zoom(saved.zoom);
                pz.pan(saved.pan);
            } catch { /* ignore */ }
        } else {
            pz.fit();
            pz.center();
        }
    }

    _scheduleDotRender(dotId, dotSource) {
        // Store the source so we can re-render after step expand or poll refresh
        this._pendingDotRenders = this._pendingDotRenders || {};
        this._pendingDotRenders[dotId] = dotSource;
        // Try to render after a short delay (DOM needs to be inserted first)
        const attempt = () => {
            const vp = document.getElementById(dotId + '-viewport');
            if (!vp) return; // Element not in DOM yet
            if (vp.querySelector('svg.dot-rendered')) return; // Already rendered
            // Check if viewport is visible (has dimensions)
            if (vp.offsetHeight > 0) {
                this._renderDotGraph(dotId, dotSource);
            } else {
                // Not visible yet (e.g., inside collapsed step panel) — retry later
                // Will be triggered by toggleStepPanel when panel opens
            }
        };
        setTimeout(attempt, 150);
    }

    async _renderDotGraph(containerId, dotSource) {
        const viewport = document.getElementById(containerId + '-viewport');
        if (!viewport) return;
        if (viewport.querySelector('svg.dot-rendered')) return;

        // Restore from cache if available (instant, no WASM needed)
        const cacheKey = containerId;
        if (this._renderedDotCache[cacheKey]) {
            viewport.innerHTML = this._renderedDotCache[cacheKey];
            const svg = viewport.querySelector('svg.dot-rendered');
            if (svg) {
                try {
                    const pz = svgPanZoom(svg, {
                        zoomEnabled: true, controlIconsEnabled: false, dblClickZoomEnabled: false,
                        fit: false, center: false, minZoom: 0.1, maxZoom: 20, zoomScaleSensitivity: 0.3,
                    });
                    this._dotPanZoom[containerId] = pz;
                    setTimeout(() => { pz.resize(); this._restoreDotViewState(containerId, pz); }, 100);
                } catch { /* ok */ }
            }
            return;
        }

        try {
            if (!this._vizInstance) {
                this._vizInstance = await Viz.instance();
            }
            const svg = this._vizInstance.renderSVGElement(dotSource);
            svg.classList.add('dot-rendered');
            svg.removeAttribute('width');
            svg.removeAttribute('height');
            svg.style.width = '100%';
            svg.style.height = '100%';
            viewport.innerHTML = '';
            viewport.appendChild(svg);

            // Cache the rendered SVG HTML for instant restore on poll refresh
            this._renderedDotCache[cacheKey] = viewport.innerHTML;

            const pz = svgPanZoom(svg, {
                zoomEnabled: true, controlIconsEnabled: false, dblClickZoomEnabled: false,
                fit: true, center: true, minZoom: 0.1, maxZoom: 20, zoomScaleSensitivity: 0.3,
            });
            this._dotPanZoom[containerId] = pz;
            setTimeout(() => { pz.resize(); pz.fit(); pz.center(); }, 100);
        } catch (e) {
            viewport.innerHTML = `<div style="color:var(--red);padding:10px;font-size:12px;">Failed to render graph: ${this._esc(e.message)}</div>`;
        }
    }

    dotFit(id) { const pz = this._dotPanZoom?.[id]; if (pz) { pz.resize(); pz.fit(); pz.center(); } }
    dotZoomIn(id) { const pz = this._dotPanZoom?.[id]; if (pz) pz.zoomIn(); }
    dotZoomOut(id) { const pz = this._dotPanZoom?.[id]; if (pz) pz.zoomOut(); }

    _renderSimpleMarkdown(text) {
        let html = this._esc(text);
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="md-code"><code>$2</code></pre>');
        html = html.replace(/^(#{1,6})\s+(.+)$/gm, (_, hashes, content) => `<strong class="md-h">${content}</strong>`);
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        html = html.replace(/^- (.+)$/gm, '<span class="md-li">\u2022 $1</span>');
        html = html.replace(/\n/g, '<br>');
        return html;
    }

    async copyFromElement(elementId, btn) {
        const el = document.getElementById(elementId);
        if (!el) return;
        const text = el.textContent;
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = document.createElement('textarea');
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        btn.classList.add('copied');
        setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 1500);
    }

    _renderTaskTable(tasks) {
        let html = `
        <table class="task-table">
            <thead><tr>
                <th style="width:40px">#</th><th>Task</th><th style="width:60px">Status</th>
                <th style="width:80px">Commit</th><th>Message</th>
            </tr></thead><tbody>`;

        for (const t of tasks) {
            let statusIcon, statusCls;
            switch (t.status) {
                case 'done': statusIcon = '\u2713'; statusCls = 'done'; break;
                case 'active': statusIcon = '\u2192'; statusCls = 'active'; break;
                default: statusIcon = '\u2013'; statusCls = 'pending';
            }
            html += `<tr>
                <td style="text-align:right;color:var(--text-muted);">${t.number}</td>
                <td>${this._esc(t.description)}</td>
                <td><span class="task-status ${statusCls}">${statusIcon}</span></td>
                <td>${t.commit_hash ? `<span class="commit-hash">${t.commit_hash}</span>` : ''}</td>
                <td>${t.commit_subject ? `<span class="commit-subject" title="${this._esc(t.commit_subject)}">${this._esc(t.commit_subject)}</span>` : ''}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        return html;
    }

    _renderRecentCommits(commits) {
        let html = '<div class="recent-commits"><h3 class="section-label">Recent Commits</h3>';
        for (const c of commits) {
            const absTime = c.timestamp ? new Date(c.timestamp).toLocaleString() : '';
            html += `<div class="commit-line">
                <span class="commit-hash">${c.hash}</span>
                <span style="color:var(--text);">${this._esc(c.subject)}</span>
                <span style="color:var(--text-muted);" title="${absTime}">${this._timeAgo(c.timestamp)}</span>
            </div>`;
        }
        html += '</div>';
        return html;
    }

    // -- Phase 2: Completed Tasks Accordion --------------------------------

    _renderCompletedTasks(session) {
        const tasks = session.completed_tasks || [];
        if (tasks.length === 0) return '';

        const allLabel = this._allTasksExpanded ? 'Collapse All' : 'Expand All';
        let html = `<div class="completed-tasks-section">
            <div class="tasks-section-header">
                <span class="tasks-section-label">Completed Tasks (${tasks.length})</span>
                <button class="expand-all-btn" onclick="event.stopPropagation();dashboard.toggleAllTasks()">${allLabel}</button>
            </div>
            <div class="completed-tasks-list">`;

        for (const task of tasks) {
            const isOpen = this._expandedTasks.has(task.index);
            const chevron = isOpen ? '\u25be' : '\u25b8';
            html += `<div class="task-card">
                <div class="task-card-header" onclick="dashboard.toggleTask(${task.index})">
                    <span class="task-card-chevron">${chevron}</span>
                    <span class="task-card-icon">\u2713</span>
                    <span class="task-card-title">${this._esc(task.title)}</span>
                </div>
                <div class="task-card-body" id="task-body-${task.index}" style="display:${isOpen ? 'block' : 'none'}">
                    <div class="md-content">${this._renderSimpleMarkdown(task.report)}</div>
                </div>
            </div>`;
        }
        html += '</div></div>';
        return html;
    }

    toggleTask(idx) {
        if (this._expandedTasks.has(idx)) {
            this._expandedTasks.delete(idx);
        } else {
            this._expandedTasks.add(idx);
        }
        const body = document.getElementById(`task-body-${idx}`);
        if (!body) return;
        const isOpen = this._expandedTasks.has(idx);
        body.style.display = isOpen ? 'block' : 'none';
        // Update chevron
        const card = body.closest('.task-card');
        if (card) {
            const chev = card.querySelector('.task-card-chevron');
            if (chev) chev.textContent = isOpen ? '\u25be' : '\u25b8';
        }
    }

    toggleAllTasks() {
        this._allTasksExpanded = !this._allTasksExpanded;
        if (this._allTasksExpanded) {
            document.querySelectorAll('.task-card-body').forEach((el, i) => {
                el.style.display = 'block';
                this._expandedTasks.add(i);
            });
            document.querySelectorAll('.task-card-chevron').forEach(el => el.textContent = '\u25be');
        } else {
            document.querySelectorAll('.task-card-body').forEach((el, i) => {
                el.style.display = 'none';
                this._expandedTasks.delete(i);
            });
            document.querySelectorAll('.task-card-chevron').forEach(el => el.textContent = '\u25b8');
        }
        // Update the button text
        const btn = document.querySelector('.expand-all-btn');
        if (btn) btn.textContent = this._allTasksExpanded ? 'Collapse All' : 'Expand All';
    }

    // -- Phase 3: Outcome Tabs --------------------------------------------

    _renderOutcomeTabs(session) {
        const tabs = [
            { id: 'summary', label: 'Summary', content: session.execution_summary },
            { id: 'review', label: 'Review', content: session.final_review },
            { id: 'verification', label: 'Verification', content: session.verification_results },
            { id: 'approval', label: 'Approval', content: session.approval_prep },
            { id: 'completion', label: 'Completion', content: session.completion_report },
        ].filter(t => t.content);

        if (tabs.length === 0) return '';

        // Default to first available tab if active tab doesn't have content
        if (!tabs.find(t => t.id === this._activeOutcomeTab)) {
            this._activeOutcomeTab = tabs[0].id;
        }

        let html = '<div class="outcome-section">';
        html += '<div class="outcome-tabs">';
        for (const tab of tabs) {
            const active = tab.id === this._activeOutcomeTab ? ' active' : '';
            html += `<button class="outcome-tab${active}" data-tab="${tab.id}" onclick="dashboard.setOutcomeTab('${tab.id}')">${tab.label}</button>`;
        }
        html += '</div>';

        for (const tab of tabs) {
            const display = tab.id === this._activeOutcomeTab ? 'block' : 'none';
            html += `<div class="outcome-panel" data-tab="${tab.id}" style="display:${display}">
                <div class="md-content">${this._renderSimpleMarkdown(tab.content)}</div>
            </div>`;
        }
        html += '</div>';
        return html;
    }

    setOutcomeTab(tabId) {
        this._activeOutcomeTab = tabId;
        document.querySelectorAll('.outcome-tab').forEach(t =>
            t.classList.toggle('active', t.dataset.tab === tabId));
        document.querySelectorAll('.outcome-panel').forEach(p =>
            p.style.display = p.dataset.tab === tabId ? 'block' : 'none');
    }

    // -- Phase 4: Approval Timeline ---------------------------------------

    _renderApprovalTimeline(session) {
        const history = session.approval_history || [];
        const approvals = session.stage_approvals || {};
        if (history.length === 0 && Object.keys(approvals).length === 0) return '';

        let html = '<div class="approval-timeline">';

        // Render from stage_approvals (stage name → decision)
        for (const [stage, decision] of Object.entries(approvals)) {
            const isApproved = typeof decision === 'string'
                ? decision.toLowerCase().includes('approv')
                : (decision && decision.approved);
            const icon = isApproved ? '\u2713' : '\u2717';
            const cls = isApproved ? 'approved' : 'denied';
            const message = typeof decision === 'string' ? decision : (decision.message || decision.reason || '');
            html += `<div class="approval-event ${cls}">
                <span class="approval-event-icon">${icon}</span>
                <span class="approval-event-stage">${this._esc(stage)}</span>
                <span class="approval-event-decision">${isApproved ? 'Approved' : 'Denied'}</span>
                ${message ? `<span class="approval-event-message">${this._esc(String(message))}</span>` : ''}
            </div>`;
        }

        // Render from approval_history array (richer timeline)
        for (const event of history) {
            if (typeof event !== 'object') continue;
            const action = event.action || event.decision || '';
            const stage = event.stage || event.stage_name || '';
            const isApproved = action.toLowerCase().includes('approv');
            const isDenied = action.toLowerCase().includes('den');
            const isWaiting = action.toLowerCase().includes('wait') || action.toLowerCase().includes('pend');
            let icon, cls;
            if (isApproved) { icon = '\u2713'; cls = 'approved'; }
            else if (isDenied) { icon = '\u2717'; cls = 'denied'; }
            else if (isWaiting) { icon = '\u23f8'; cls = 'waiting'; }
            else { icon = '\u25cf'; cls = ''; }

            const timestamp = event.timestamp || event.time || '';
            const timeStr = timestamp ? this._timeAgo(timestamp) : '';
            const message = event.message || event.reason || '';

            html += `<div class="approval-event ${cls}">
                <span class="approval-event-icon">${icon}</span>
                <span class="approval-event-stage">${this._esc(stage)}</span>
                <span class="approval-event-decision">${this._esc(action)}</span>
                ${timeStr ? `<span class="approval-event-time">${timeStr}</span>` : ''}
                ${message ? `<span class="approval-event-message">${this._esc(String(message))}</span>` : ''}
            </div>`;
        }

        // If currently waiting, show pending event
        if (session.pending_approval_stage && session.status === 'waiting') {
            html += `<div class="approval-event waiting">
                <span class="approval-event-icon">\u23f8</span>
                <span class="approval-event-stage">${this._esc(session.pending_approval_stage)}</span>
                <span class="approval-event-decision">Pending</span>
            </div>`;
        }

        html += '</div>';
        return html;
    }

    _renderContextSummary(session) {
        const ctx = session.context_summary;
        if (!ctx || Object.keys(ctx).length === 0) return '';

        let html = '<table class="context-table"><tbody>';
        for (const [key, value] of Object.entries(ctx)) {
            const valStr = typeof value === 'string' ? value : JSON.stringify(value);
            const stableId = `ctx-${key}`;
            html += `<tr>
                <td class="context-key">${this._esc(key)}</td>
                <td class="context-value">${this._renderExpandableValue(valStr, stableId)}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        return html;
    }

    // -- Navigation -------------------------------------------------------

    navigateTo(sessionId) {
        window.location.hash = sessionId;
    }

    navigateBack() {
        if (window.history.length > 1) {
            history.back();
        } else {
            window.location.hash = '';
        }
    }

    // -- Helpers -----------------------------------------------------------

    _shortPath(projectPath, fallbackSlug) {
        // Use the real filesystem path when available — slug-to-path
        // conversion is lossy (hyphens in directory names are
        // indistinguishable from path separators in the slug).
        if (projectPath) {
            return projectPath.replace(/^\/(?:Users|home)\/[^/]+\//, '~/');
        }
        // Fallback: strip the Users-<username>- prefix, keep hyphens as-is.
        return (fallbackSlug || '').replace(/^Users-[^-]+-/, '') || fallbackSlug || '';
    }

    _esc(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    _timeAgo(isoStr) {
        if (!isoStr) return '';
        const then = new Date(isoStr);
        const now = new Date();
        const diffMs = now - then;
        const diffSec = Math.floor(diffMs / 1000);
        const diffMin = Math.floor(diffSec / 60);
        const diffHour = Math.floor(diffMin / 60);
        const diffDay = Math.floor(diffHour / 24);

        if (diffSec < 60) return `${diffSec}s ago`;
        if (diffMin < 60) return `${diffMin}m ago`;
        if (diffHour < 24) return `${diffHour}h ${diffMin % 60}m ago`;
        return `${diffDay}d ago`;
    }
}

const dashboard = new RecipeDashboard();
