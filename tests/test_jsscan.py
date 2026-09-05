# audited on 20260903
import unittest

from countersign.jsscan import empty_functions, mask_source

FLAGGED = {
    "function declaration": ("export function sendInvoice(orderId: string): void {}\n", [(1, "sendInvoice")]),
    "async function declaration": ("async function syncLedger() {\n}\n", [(1, "syncLedger")]),
    "generator declaration": ("function* pages() { }\n", [(1, "pages")]),
    "default export function": ("export default function () {}\n", [(1, "default")]),
    "body with whitespace only": ("function reconcile(a, b) {\n\n}\n", [(1, "reconcile")]),
    "class method": ("class Billing {\n  charge(amount: number): Promise<void> {}\n}\n", [(2, "charge")]),
    "async class method": ("class Billing {\n  async refund(id: string) {\n  }\n}\n", [(2, "refund")]),
    "public static method": ("class Billing {\n  public static fromConfig(cfg: Config): Billing {}\n}\n", [(2, "fromConfig")]),
    "generic method": ("class Store {\n  put<T>(key: string, value: T) {}\n}\n", [(2, "put")]),
    "object literal method": ("export const handlers = {\n  onError(err: Error) {},\n  onClose() { close(); },\n};\n", [(2, "onError")]),
    "exported arrow function": ("export const notify = async (userId: string) => {};\n", [(1, "notify")]),
    "exported arrow with return type": ("export const load = (): Promise<void> => {\n};\n", [(1, "load")]),
    "getter": ("class A {\n  get total(): number {}\n}\n", [(2, "total")]),
    "exported function expression": ("export const donothing = function () {};\n", [(1, "donothing")]),
    "empty object type then an empty body": ("class A {\n  f(): {} {}\n}\n", [(2, "f")]),
    "exported generic arrow": ("export const id = <T,>(x: T) => {};\n", [(1, "id")]),
    "exported arrow with typed binding": ("export const h: Handler = async (req) => {};\n", [(1, "h")]),
    "two in one file": ("function a() {}\nfunction b() { return 1; }\nfunction c() {}\n", [(1, "a"), (3, "c")]),
}

NOT_FLAGGED = {
    "documented no-op with a line comment": "function reconcile(a, b) {\n  // nothing to reconcile: the ledger is append-only\n}\n",
    "documented no-op with a block comment": "class A {\n  onClose() { /* intentionally empty */ }\n}\n",
    "constructor with parameter properties": "class Api {\n  constructor(private readonly http: HttpClient) {}\n}\n",
    "angular lifecycle hook": "class Page implements OnInit {\n  ngOnInit(): void {}\n}\n",
    "control flow blocks": "function f(x) {\n  if (x) {}\n  for (const y of x) {}\n  while (x) {}\n  switch (x) {}\n  try {} catch (e) {}\n  do {} while (x);\n  return x;\n}\n",
    "function text inside a string": "const s = 'function fake() {}';\nconst t = \"function fake2() {}\";\n",
    "function text inside a comment": "// function fake() {}\n/* function fake2() {} */\nfunction real() { return 1; }\n",
    "function text inside a template literal": "const q = `select ${render()} where function fake() {}`;\n",
    "regex literal containing braces": "const r = /function x\\(\\) \\{\\}/g;\nconst s = a / b;\n",
    "callback arrow passed to a call": "useEffect(() => {}, []);\nitems.forEach(() => {});\n",
    "unexported arrow": "const onChange = () => {};\n",
    "unexported function expression": "const donothing = function() {};\nlisten(function () {});\n",
    "exported noop function expression": "export const noop = function () {};\n",
    "exported noop": "export const noop = () => {};\nexport const noOp = () => {};\n",
    "overload signatures": "function parse(x: number): number;\nfunction parse(x: string): string;\nfunction parse(x: any) { return x; }\n",
    "abstract method": "abstract class Base {\n  abstract run(): void;\n}\n",
    "interface methods": "interface Reader {\n  read(): string;\n  close(): void;\n}\n",
    "declare function": "declare function load(): void;\n",
    "empty object type as return type": "function shape(): {} { return build(); }\n",
    "interface method returning the empty object type": "interface R {\n  read(): {};\n}\n",
    "union with the empty object type": "function shape(): {} | null { return build(); }\n",
    "array of object type": "function rows(): { a: number }[] { return []; }\n",
    "exported ternary holding an empty arrow": "export const handler = isTest ? () => {} : real;\n",
    "exported arrow whose body has code": "export const load = async (): Promise<void> => { await go(); };\n",
    "empty class": "export class Marker {}\n",
    "empty interface and namespace": "interface Empty {}\nnamespace N {}\nenum E {}\n",
    "call followed by block on next line": "log(\n  x\n);\n{\n}\n",
    "method with body": "class A {\n  run() {\n    start();\n  }\n}\n",
    "await and new with parens": "async function f() {\n  await (async () => {})();\n  new Foo();\n}\n",
    "object literal method with body": "const h = { start() { go(); } };\n",
    "arrow returning object": "export const make = () => ({});\n",
    "jsx component with body": "export function Page() {\n  return <div>{}</div>;\n}\n",
    "empty block inside a body": "function f() {\n  {}\n  return 1;\n}\n",
}


class TestMask(unittest.TestCase):
    def test_mask_keeps_line_count_and_blanks_contents(self):
        source = "const a = 'x\\'y';\n// c\nconst b = `t${a}`;\n/* multi\nline */\nconst r = /[}]/;\n"
        masked = mask_source(source)
        self.assertEqual(masked.count("\n"), source.count("\n"))
        self.assertEqual(len(masked), len(source))
        self.assertNotIn("x", masked.split("\n")[0])
        self.assertNotIn("c", masked.split("\n")[1])
        self.assertNotIn("multi", masked)
        self.assertNotIn("}", masked.split("\n")[5])


class TestEmptyFunctions(unittest.TestCase):
    def test_flagged_cases(self):
        for label, (source, expected) in FLAGGED.items():
            with self.subTest(label):
                self.assertEqual(empty_functions(source), expected)

    def test_not_flagged_cases(self):
        for label, source in NOT_FLAGGED.items():
            with self.subTest(label):
                self.assertEqual(empty_functions(source), [])

    def test_minified_looking_source_is_skipped(self):
        source = "function a(){}" + ";x=1" * 600 + "\n"
        self.assertEqual(empty_functions(source), [])

    def test_crlf_line_numbers(self):
        self.assertEqual(empty_functions("const x = 1;\r\nfunction a() {}\r\n"), [(2, "a")])


if __name__ == "__main__":
    unittest.main()
