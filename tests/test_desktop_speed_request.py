import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from desktop_speed_request_patches import (  # noqa: E402
    HOOK_MARKER, REQUEST_MARKER, PatchError, discover_speed_request_patches,
)


HOOK = '''uizX(e,t,n){n.d(t,{k:()=>E});function E(){
let account=(0,d.tn)(host),old=(0,i._U)(enabled?v.J8:null,args),ee=old?.data;
let{serviceTierAccess:access}=(0,p.U)(args);
if(access){};void {serviceTierForRequest:access};setDefaultServiceTierConfig();updateThreadSettingsForNextTurn();
clearPrewarmedThreads():void 0
}}'''

REQUEST = '''PAzX(e,t,n){var r=n("eWgO"),i=n("Dgwo"),a=n("tl5a"),s=n("wCOm"),o=n("mGK9"),l=n("B81s"),u=n("WFY2"),d=n("l2g8");
async function c(e,t,n,r){try{let i=await p(e,t);if(void 0!==r)return(0,d.E_)(null,r,i);let{config:l}=await (0,s.ig)(e,t,{includeLayers:!1,cwd:null},{priority:"critical"}),u=(0,s.Y$)(l);return(0,d.E_)(null,u.service_tier,i)}catch(e){void "Failed to read service tier for request";return null}}
async function p(e,t){let n=await (0,o.s)(e,t);if("chatgpt"!==n&&"personalAccessToken"!==n&&"amazonBedrock"!==n)return null;let i=await (0,l.Wr)(e,t,{priority:"critical"});return e.query.setData(l.b9,{authMethod:n,hostId:t},i),(0,r.W6)(i)}
async function h(){throw Error("Failed to read service tier model")}; n.d(t,{e:()=>c})}'''

INLINED_REQUEST = '''PAzX(e,t,n){var r=n("eWgO"),i=n("Dgwo"),a=n("tl5a"),s=n("wCOm"),o=n("mGK9"),l=n("B81s"),u=n("WFY2"),d=n("l2g8");
n.d(t,{e:async function(e,t,n,c){try{let p=await async function(e,t){let n=await (0,o.lookup)(e,t);if("chatgpt"!==n&&"personalAccessToken"!==n&&"amazonBedrock"!==n)return null;let i=await (0,l.Wr)(e,t,{priority:"critical"});return e.query.setData(l.b9,{authMethod:n,hostId:t},i),(0,r.W6)(i)}(e,t);
if(void 0!==c)return(0,d.E_)(null,c,p);let h=e.get(a.QN,t);if("fromConfig"!==h.type)return(0,d.E_)(null,(0,d.qh)(h,null),p);
let{config:g}=await (0,s.readConfig)(e,t,{includeLayers:!1,cwd:null},{priority:"critical"}),f=(0,s.resolveConfig)(g);if(null!=f.service_tier)return(0,d.E_)(null,f.service_tier,p);
return(0,d.E_)(await async function(){throw Error("Failed to read service tier model")}(),f.service_tier,p)}catch(e){void "Failed to read service tier for request";return null}}})}'''


def run_node(script):
    return json.loads(subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True).stdout)


class SpeedRequestTests(unittest.TestCase):
    def setUp(self):
        self.patched = discover_speed_request_patches({'hook.js': HOOK.encode(), 'request.js': REQUEST.encode()})

    def test_hook_api_feature_and_subscription_behavior(self):
        body = self.patched['hook.js'].decode()
        patch = body[body.index('/*' + HOOK_MARKER):body.index('if(access)')]
        result = run_node('''
const hook = (account, config, original) => {
  let access=original;
  const __duoFastAccess=Object.freeze({fast:true,ultrafast:false,orbitApi:true});
  const i={_U: () => ({data: config})}, v={J8: true}, args={};
  ''' + patch + '''
  return access;
};
console.log(JSON.stringify([
  hook({authMethod:'apikey'}, {features:{fast_mode:true}}, null),
  hook({authMethod:'apikey'}, {features:{fast_mode:false}}, {fast:true}),
  hook({authMethod:'apikey'}, null, {fast:true}),
  hook({authMethod:'chatgpt'}, {features:{fast_mode:false}}, {fast:true,ultrafast:true})
]));''')
        self.assertEqual(result, [{'fast': True, 'ultrafast': False, 'orbitApi': True}, None, None,
                                  {'fast': True, 'ultrafast': True}])

    def test_request_uses_config_and_respects_login(self):
        module = self.patched['request.js'].decode()
        result = run_node('''
let auth, config;
const modules = {
 eWgO:{W6:x=>x}, Dgwo:{}, tl5a:{},
 wCOm:{ig:async()=>({config}), 'Y$':x=>x},
 mGK9:{s:async()=>auth}, B81s:{Wr:async()=>({fast:true}), b9:'b9'},
 WFY2:{}, l2g8:{E_:(_,tier,access)=>tier==='default'?null:access?.fast?tier:null}
};
const n=id=>modules[id]; n.d=(t,x)=>{for(const [key,get] of Object.entries(x)) Object.defineProperty(t,key,{get})};
const factory=({''' + module + '''}).PAzX, exports={}; factory(null,exports,n);
const store={query:{setData(){}}};
(async()=>{
  let out=[];
  for (const [a,c] of [
   ['apikey',{features:{fast_mode:true}}], ['apikey',{features:{fast_mode:false}}],
   ['chatgpt',{features:{fast_mode:false}}], ['unknown',{features:{fast_mode:true}}]
  ]) {auth=a;config=c;out.push(await exports.e(store,'local',null,'priority'));}
  console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1)});''')
        self.assertEqual(result, ['priority', None, 'priority', None])

    def test_factory_identifiers_do_not_select_the_patch(self):
        renamed = discover_speed_request_patches({
            'hook.js': HOOK.replace('uizX(', 'renamedHook(').encode(),
            'request.js': REQUEST.replace('PAzX(', 'renamedRequest(').encode(),
        })
        self.assertIn(HOOK_MARKER.encode(), renamed['hook.js'])
        self.assertIn(REQUEST_MARKER.encode(), renamed['request.js'])

    def test_inlined_request_and_renamed_exports_preserve_behavior(self):
        patched = discover_speed_request_patches({'hook.js': HOOK.encode(),
                                                   'request.js': INLINED_REQUEST.encode()})
        module = patched['request.js'].decode()
        result = run_node('''
let auth, config;
const modules = {
 eWgO:{W6:x=>x}, Dgwo:{}, tl5a:{QN:'config'},
 wCOm:{readConfig:async()=>({config}), resolveConfig:x=>x},
 mGK9:{lookup:async()=>auth}, B81s:{Wr:async()=>({fast:true}), b9:'b9'},
 WFY2:{}, l2g8:{E_:(_,tier,access)=>tier==='default'?null:access?.fast?tier:null}
};
const n=id=>modules[id]; n.d=(t,x)=>{for(const [key,value] of Object.entries(x))t[key]=value};
const factory=({''' + module + '''}).PAzX, exports={}; factory(null,exports,n);
const store={get(){return {type:'fromConfig'}},query:{setData(){}}};
(async()=>{
 let out=[];
 for (const [a,c] of [
  ['apikey',{features:{fast_mode:true}}], ['apikey',{features:{fast_mode:false}}],
  ['chatgpt',{features:{fast_mode:false}}], ['unknown',{features:{fast_mode:true}}]
 ]) {auth=a;config=c;out.push(await exports.e(store,'local',null,'priority'));}
 console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exit(1)});''')
        self.assertEqual(result, ['priority', None, 'priority', None])

    def test_missing_contract_rejected(self):
        with self.assertRaises(PatchError):
            discover_speed_request_patches({'hook.js': HOOK.encode()})
        self.assertIn(REQUEST_MARKER.encode(), self.patched['request.js'])


if __name__ == '__main__':
    unittest.main()
