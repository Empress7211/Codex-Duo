import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))

from desktop_renderer_patches import (_patch_host_events, _patch_release,
                                      _patch_resume, _patch_numeric_input_gates,
                                      _patch_numeric_global_input)


class ReleasePatchTests(unittest.TestCase):
    def test_normalization_variants_keep_release_in_status_handler(self):
        for normalize in ('normal', '(0,_.J)'):
            with self.subTest(normalize=normalize):
                text = (
                    'x.inactiveThreadUnsubscriber.clearConversationStreamOwnership(a);'
                    'switch(n.method){case"thread/status/changed":{'
                    'let{threadId:r,status:i}=n.params;if(!i)return;'
                    f'let a={normalize}(r);'
                    'if(!t.threadStore.conversations.has(a)){'
                    'let x=t.threadStore.recordThreadRuntimeStatusEvidence(r,i);return}'
                    'e.updateConversationState(a,x=>x);return}}'
                )
                patched = _patch_release(text)
                self.assertIn(f'let a={normalize}(r);/*orbit-renderer-writer-release*/', patched)
                self.assertIn('if(n.params.orbitReleased===!0)', patched)
                self.assertIn('e.inactiveThreadUnsubscriber.clearConversationStreamOwnership(a)', patched)
                self.assertIn("detail:{threadId:r,has_writer:!1}", patched)

    def test_host_event_uses_outer_payload_after_nested_switch(self):
        text = ('switch(e.type){case"nested":switch(n.type){case"x":break;}break;'
                'case"workspace-root-options-updated":refresh();break;'
                'case"global-state-updated":for(let k of e.keys)update(k);break;}')
        patched = _patch_host_events(text)
        self.assertIn("new CustomEvent('orbit-writer-locks-changed',{detail:e})", patched)
        self.assertNotIn("new CustomEvent('orbit-writer-locks-changed',{detail:n})", patched)

    def test_compiled_resume_binds_state_and_writer_event(self):
        text = ('function resume(thread){let [loading,setLoading]=(0,R.useState)(wanted);'
                'let inFlight=wanted&&loading,held=wanted&&null!=thread&&conflict===thread;'
                'return cached?{isResuming:inFlight,hasResumeFailed:x,'
                'isWriterConflict:held,retryResume:retry}:cached}'
                'function message(formatter){return formatter.formatMessage({'
                'id:"localTaskRow.resumeLiveWriterError",defaultMessage:"active elsewhere",})}')
        patched = _patch_resume(text)
        self.assertIn('(0,R.useLayoutEffect)(()=>{setLoading(wanted)},[thread,wanted])', patched)
        self.assertIn('orbitEvent.detail?.threadId===thread&&conflict===thread', patched)
        self.assertIn('retry()', patched)

    def test_resume_loading_respects_config_error(self):
        text = ('function resume(thread){let [loading,setLoading]=(0,R.useState)(wanted&&null==error);'
                'let inFlight=wanted&&loading,held=wanted&&null!=thread&&conflict===thread;'
                'return cached?{isResuming:inFlight,hasResumeFailed:x,'
                'isWriterConflict:held,retryResume:retry}:cached}'
                'function message(formatter){return formatter.formatMessage({'
                'id:"localTaskRow.resumeLiveWriterError",defaultMessage:"active elsewhere",})}')
        patched = _patch_resume(text)
        self.assertIn('(0,R.useLayoutEffect)(()=>{setLoading(wanted&&null==error)},'
                      '[thread,wanted,error])', patched)

    def test_compiled_input_gates_track_resume(self):
        text = (
            'let{isResuming:H,hasResumeFailed:F,isUnavailable:U,hasReadOnlyAccess:V,'
            'isWriterConflict:G,environmentStatus:E,retryResume:Q}=hook;'
            'let Z=!T||G||V,Y=H&&!G,J=T&&!G&&!V;'
            'let{isResuming:r,hasResumeFailed:f,isUnavailable:u,hasReadOnlyAccess:a,'
            'isWriterConflict:c,environmentStatus:e,retryResume:q}=other;'
            'let retry=fn,readonly=x||blocked||c||!0===a;'
            'let show=ready&&!blocked&&!c&&!a;'
            'function composer(props){let{isThreadHistoryLoading:loading}=props;'
            'return {submitDisabled:S||Y||handoff,isThreadHandoffInProgress:handoff}}'
            'let title={id:"localConversation.writerConflict.title",defaultMessage:"title",};'
            'let description={id:"localConversation.writerConflict.description",defaultMessage:"description",};'
        )
        patched = _patch_numeric_input_gates(text)
        self.assertIn('let Z=!T||G||V||H,Y=H&&!G,J=T&&!G&&!V&&!H;', patched)
        self.assertIn('readonly=x||blocked||c||!0===a||r;', patched)
        self.assertIn('show=ready&&!blocked&&!c&&!a&&!r;', patched)
        self.assertIn('submitDisabled:S||Y||handoff||loading', patched)

    def test_global_entry_disables_submit_while_resuming(self):
        text = ('let{isResuming:r,hasResumeFailed:f,isUnavailable:u,isWriterConflict:c,'
                'hasReadOnlyAccess:a,retryResume:retry}=hook;'
                'let props={isThreadHistoryLoading:r||other,submitDisabled:f||u||c||a,more:true};')
        patched = _patch_numeric_global_input(text)
        self.assertIn('submitDisabled:f||u||c||a||r,', patched)

    def test_provider_aware_input_gates_track_resume(self):
        text = (
            'let{isResuming:H,hasResumeFailed:F,isUnavailable:U,hasReadOnlyAccess:V,'
            'isWriterConflict:G,providerBlock:P,isReadyForInteraction:R,environmentStatus:E,retryResume:Q}=hook;'
            'let Z=!T||V||G||!R,Y=H&&!G&&null==P,J=T&&!V&&!G&&null==P;'
            'let{isResuming:r,hasResumeFailed:f,isUnavailable:u,hasReadOnlyAccess:a,'
            'isWriterConflict:c,providerBlock:p,isReadyForInteraction:k,environmentStatus:e}=other;'
            'let surface=x,history=r&&!c&&null==p,readonly=blocked||!k,resuming=r&&!c&&null==p,submit=!k;'
            'let show=ready&&!blocked&&!c&&!a&&null==p;'
            'function composer(props){let{isThreadHistoryLoading:loading}=props;'
            'return {submitDisabled:S||Y||handoff,isThreadHandoffInProgress:handoff}}'
            'let title={id:"localConversation.writerConflict.title",defaultMessage:"title",};'
            'let description={id:"localConversation.writerConflict.description",defaultMessage:"description",};'
        )
        patched = _patch_numeric_input_gates(text)
        self.assertIn('Z=!T||V||G||!R||H', patched)
        self.assertIn('J=T&&!V&&!G&&null==P&&!H', patched)
        self.assertIn('readonly=blocked||!k||r', patched)
        self.assertIn('submit=!k||r', patched)
        self.assertIn('show=ready&&!blocked&&!c&&!a&&null==p&&!r', patched)
        self.assertIn('submitDisabled:S||Y||handoff||loading', patched)


if __name__ == '__main__':
    unittest.main()
