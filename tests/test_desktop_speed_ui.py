import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from desktop_speed_ui_patches import _patch_settings, _patch_speed_controls


class SpeedUiTests(unittest.TestCase):
    def test_control_matches_associated_announcement_across_jsx_shapes(self):
        for helper, children in [('jsx', 'warning'), ('jsxs', '[title," ",warning]')]:
            with self.subTest(helper=helper):
                script = (
                    'option=>"fast"===option.iconKind?{...option,label:"composer.intelligencePicker.fastMode.advanced.title"};'
                    '(0,r.jsx)(other,{className:x.KeyboardAnnouncement,id:other,children:ignored});'
                    '{"aria-checked":checked,"aria-describedby":description,"aria-label":label,className:x.FastModeToggle,'
                    'tooltipText:(0,r.jsxs)("span",{children:(0,r.jsx)(x,{id:"composer.intelligencePicker.fastMode.speed.tooltip.withMultiplier",'
                    'values:{speedMultiplier:fast.speedMultiplier??1.5}})}),variant:"unstyled",'
                    f'children:(0,r.{helper})("span",{{className:x.KeyboardAnnouncement,id:description,children:{children}}})' + '}'
                )
                patched = _patch_speed_controls(script)
                self.assertIn(f'children:fast.orbitApi?null:{children}', patched)
                self.assertIn('id:other,children:ignored', patched)
                self.assertIn('fast.orbitApi?(checked?', patched)

    def test_settings_recognizes_renamed_compiler_exports(self):
        script = ('/*orbit-api-speed-request-hook*/'
                  'let{serviceTierAccess:access}=(0,h.changed)(args);'
                  'allowed=option=>(0,f.renamed)(option.value,access),'
                  'options=(0,g.catalog)(model).filter(allowed)')
        patched = _patch_settings(script)
        self.assertIn('options=(0,g.catalog)(model).filter(allowed).map(', patched)
        self.assertIn('access?.orbitApi', patched)
        self.assertIn('Requests Fast; availability and pricing depend on the provider', patched)


if __name__ == '__main__':
    unittest.main()
