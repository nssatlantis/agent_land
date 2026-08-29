        + f'<div id="frag-stake-list">{_stake_page_rows(stakes)}</div>'
        + '<script>function _toggleStakeLocks(sId){var e=document.getElementById("stake-locks-"+sId);if(e)e.style.display=e.style.display==="none"?"block":"none"}</script>'
        + _pager(page, total_pages, lambda n: _staking_href(status, currency, n))