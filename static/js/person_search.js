// Live search-as-you-type person picker. Wires a text input + hidden ID field +
// results dropdown to /admin/people/search, so pages never have to load the full
// roster client-side — important once there are hundreds or thousands of people.
function initPersonSearch(opts) {
    var input   = document.getElementById(opts.inputId);
    var hidden  = document.getElementById(opts.hiddenId);
    var results = document.getElementById(opts.resultsId);
    var debounceTimer = null;
    var activeIndex = -1;
    var currentMatches = [];

    function escapeHtml(s) {
        var div = document.createElement('div');
        div.textContent = s || '';
        return div.innerHTML;
    }

    function highlight() {
        Array.prototype.forEach.call(results.children, function (child, i) {
            child.style.background = i === activeIndex ? 'var(--surface2)' : '';
        });
    }

    function selectPerson(person) {
        hidden.value = person.id;
        input.value = person.full_name;
        results.style.display = 'none';
        results.innerHTML = '';
    }

    function renderResults(matches) {
        currentMatches = matches;
        activeIndex = -1;
        results.innerHTML = '';
        if (matches.length === 0) {
            results.style.display = 'none';
            return;
        }
        matches.forEach(function (person) {
            var row = document.createElement('div');
            row.style.cssText = 'padding:.5rem .8rem;cursor:pointer;border-bottom:1px solid var(--border);';
            row.innerHTML = '<strong>' + escapeHtml(person.full_name) + '</strong>' +
                (person.site ? ' <span style="color:var(--muted);">— ' + escapeHtml(person.site) + '</span>' : '') +
                '<div style="color:var(--muted);font-size:.78rem;">' + escapeHtml(person.email) + '</div>';
            row.addEventListener('mouseenter', function () {
                activeIndex = Array.prototype.indexOf.call(results.children, row);
                highlight();
            });
            // mousedown (not click) fires before the input's blur, so the
            // dropdown doesn't close itself out from under the selection.
            row.addEventListener('mousedown', function (e) {
                e.preventDefault();
                selectPerson(person);
            });
            results.appendChild(row);
        });
        results.style.display = 'block';
    }

    function search(q) {
        fetch('/admin/people/search?q=' + encodeURIComponent(q))
            .then(function (res) { return res.json(); })
            .then(renderResults)
            .catch(function () { results.style.display = 'none'; });
    }

    input.addEventListener('input', function () {
        hidden.value = ''; // typing invalidates any prior selection until they pick again
        var q = input.value.trim();
        clearTimeout(debounceTimer);
        if (q.length < 2) {
            results.style.display = 'none';
            return;
        }
        debounceTimer = setTimeout(function () { search(q); }, 200);
    });

    input.addEventListener('keydown', function (e) {
        if (results.style.display === 'none' || currentMatches.length === 0) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            activeIndex = Math.min(activeIndex + 1, currentMatches.length - 1);
            highlight();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            activeIndex = Math.max(activeIndex - 1, 0);
            highlight();
        } else if (e.key === 'Enter') {
            if (activeIndex >= 0) {
                e.preventDefault();
                selectPerson(currentMatches[activeIndex]);
            }
        } else if (e.key === 'Escape') {
            results.style.display = 'none';
        }
    });

    document.addEventListener('click', function (e) {
        if (e.target !== input) {
            results.style.display = 'none';
        }
    });
}
