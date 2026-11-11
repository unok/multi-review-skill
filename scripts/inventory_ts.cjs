// 対象ソースはロード・実行しない。利用者が入れた依存を信頼し、typescript パッケージは実行する。
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
const DECIMAL_WITH_LEADING_ZERO = 1489;
const LEGACY_OCTAL_LITERAL = 1121;

async function analyze(request) {
  let directory = path.dirname(request.file);
  let compiler, localRequire, packageRoot;
  function resolveCompilerEntry(specifier) {
    const resolved = fs.realpathSync(localRequire.resolve(specifier));
    // Node が親の node_modules へ暗黙に代替するのを防ぐ。
    const relative = path.relative(packageRoot, resolved);
    if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
      throw new Error('resolved outside selected typescript package');
    }
    return resolved;
  }
  for (;;) {
    const candidate = path.join(directory, 'node_modules', 'typescript');
    if (fs.existsSync(path.join(candidate, 'package.json'))) {
      localRequire = createRequire(path.join(directory, '__mr_inventory__.cjs'));
      packageRoot = fs.realpathSync(candidate);
      compiler = require(resolveCompilerEntry('typescript'));
      break;
    }
    if (directory === request.root) break;
    const parent = path.dirname(directory);
    if (parent === directory || !directory.startsWith(request.root + path.sep)) break;
    directory = parent;
  }
  if (!compiler) throw new Error('typescript not found within project root');
  let ts = compiler, source, diagnostics, close = () => {};
  if (typeof ts.createSourceFile === 'function') {
    source = ts.createSourceFile(request.file, request.source, ts.ScriptTarget.Latest, true);
    diagnostics = source.parseDiagnostics;
  } else {
    // TypeScript 7 のネイティブパーサーは snapshot API 経由で使う。
    const load = name => import(pathToFileURL(resolveCompilerEntry(name)).href);
    ts = await load('typescript/unstable/ast');
    const {API} = await load('typescript/unstable/sync');
    const {createVirtualFileSystem} = await load('typescript/unstable/fs');
    const config = path.join(request.root, '__mr_inventory_tsconfig__.json');
    // ネイティブ API のプロジェクト探索も大文字拡張子を受け付けるよう、仮想パスだけ正規化する。
    const extension = path.extname(request.file);
    const file = request.file.slice(0, -extension.length) + extension.toLowerCase();
    // lib や import 先を読まず、対象ソースだけを解析する。
    const virtual = createVirtualFileSystem({
      [file]: request.source,
      [config]: JSON.stringify({compilerOptions: {noLib: true, noResolve: true, allowJs: true, jsx: 'preserve'}, files: [file]})
    });
    const api = new API({cwd: request.root, fs: virtual});
    close = () => api.close();
    try {
      const snapshot = api.updateSnapshot({openProjects: [config]});
      const program = snapshot.getProject(config).program;
      diagnostics = program.getSyntacticDiagnostics(file);
      source = program.getSourceFile(file);
      if (!source) throw new Error('typescript returned no source AST');
    } catch (error) {
      close();
      throw error;
    }
  }
  try {
    // JS の旧式数値表記は AST に残るので、TS 固有の禁止診断では解析を止めない。
    diagnostics = diagnostics.filter(d =>
      !((source.scriptKind === ts.ScriptKind.JS || source.scriptKind === ts.ScriptKind.JSX) &&
        (d.code === DECIMAL_WITH_LEADING_ZERO || d.code === LEGACY_OCTAL_LITERAL)));
    if (diagnostics.length) {
      throw new Error(diagnostics.map(d => d.text ?? ts.flattenDiagnosticMessageText(d.messageText, '\n')).join('; '));
    }
    return collect(ts, source, request);
  } finally {
    close();
  }
}

function collect(ts, source, request) {
  const result = {variables: [], declarations: [], literals: []};
  const line = node => source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
  const entry = (node, name) => ({name, line: line(node),
    endLine: source.getLineAndCharacterOfPosition(node.end).line + 1});
  const text = node => node.getText(source);
  const bindingNames = node => ts.isIdentifier(node) ? [text(node)] :
    node.elements.flatMap(element => ts.isOmittedExpression(element) ? [] : bindingNames(element.name));
  const literal = (node, kind, value) => result.literals.push({kind, value, line: line(node)});
  function walk(node, scope = [], inFunction = false) {
    // 型構文は宣言として列挙するが、実行時リテラルの収集元にはしない。
    if (ts.isExpressionWithTypeArguments(node)) {
      walk(node.expression, scope, inFunction);
      return;
    }
    if (ts.isTypeNode(node)) return;
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
      node.forEachChild(child => { if (child !== node.moduleSpecifier) walk(child, scope, inFunction); });
      return;
    }
    if (ts.isExternalModuleReference(node)) return;
    if (ts.isModuleDeclaration(node)) {
      if (node.body) walk(node.body, [...scope, text(node.name)], inFunction);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      const list = node.parent;
      const statement = list.parent;
      if (ts.isVariableDeclarationList(list) && !scope.length && !inFunction &&
          ((ts.isVariableStatement(statement) && ts.isSourceFile(statement.parent)) ||
           !(list.flags & ts.NodeFlags.BlockScoped))) {
        for (const name of bindingNames(node.name)) {
          result.variables.push({...entry(node, name), value: node.initializer ? text(node.initializer) : '［初期値なし］'});
        }
      }
    }
    let parent = node.parent;
    while (parent && (ts.isParenthesizedExpression(parent) || ts.isAsExpression(parent) ||
           parent.kind === ts.SyntaxKind.TypeAssertionExpression || ts.isSatisfiesExpression(parent) ||
           ts.isNonNullExpression(parent))) parent = parent.parent;
    const typeDeclaration = ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) || ts.isEnumDeclaration(node);
    if (typeDeclaration || ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
      const name = node.name ? text(node.name) :
        ((ts.isVariableDeclaration(parent) || ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent)) ?
          text(parent.name) : node.modifiers?.some(modifier => modifier.kind === ts.SyntaxKind.DefaultKeyword) ? 'default' : null);
      if (!name) {
        node.forEachChild(child => walk(child, scope, inFunction));
        return;
      }
      const signature = ts.isTypeAliasDeclaration(node) ? text(node) :
        request.source.slice(node.getStart(source), node.members?.pos ? node.members.pos - 1 : node.end).trim();
      result.declarations.push({...entry(node, [...scope, name].join('.')), signature});
      if (ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) return;
      node.forEachChild(child => walk(child, [...scope, name], inFunction));
      return;
    }
    if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) ||
        ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node) ||
        ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
      const name = ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name) ? text(parent.name) :
        node.name ? text(node.name) : ts.isConstructorDeclaration(node) ? 'constructor' :
        (ts.isPropertyDeclaration(parent) || ts.isPropertyAssignment(parent)) ? text(parent.name) :
          node.modifiers?.some(modifier => modifier.kind === ts.SyntaxKind.DefaultKeyword) ? 'default' : null;
      walkFunction(node, name, scope);
      return;
    }
    if (ts.isPrefixUnaryExpression(node) &&
        (node.operator === ts.SyntaxKind.MinusToken || node.operator === ts.SyntaxKind.PlusToken)) {
      let operand = node.operand;
      while (ts.isParenthesizedExpression(operand)) operand = operand.expression;
      if (ts.isNumericLiteral(operand) || ts.isBigIntLiteral(operand)) {
        literal(node, ts.isBigIntLiteral(operand) ? 'bigint' : 'number',
          (node.operator === ts.SyntaxKind.MinusToken ? '-' : '') + text(operand));
        return;
      }
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) literal(node, 'string', node.text);
    else if (ts.isNumericLiteral(node) || ts.isBigIntLiteral(node)) literal(node, ts.isBigIntLiteral(node) ? 'bigint' : 'number', text(node));
    node.forEachChild(child => walk(child, scope, inFunction));
  }
  function walkFunction(node, name, scope) {
    if (name) {
      const signature = request.source.slice(node.getStart(source), node.body ? node.body.pos : node.end).trim();
      result.declarations.push({...entry(node, [...scope, name].join('.')), signature});
    }
    const nested = name ? [...scope, name] : scope;
    // 型注釈を辿らず、実行時の式だけを辿る。
    for (const modifier of node.modifiers || []) if (ts.isDecorator(modifier)) walk(modifier.expression, nested, true);
    if (node.name && ts.isComputedPropertyName(node.name)) walk(node.name.expression, nested, true);
    for (const parameter of node.parameters) {
      for (const modifier of parameter.modifiers || []) if (ts.isDecorator(modifier)) walk(modifier.expression, nested, true);
      walk(parameter.name, nested, true);
      if (parameter.initializer) walk(parameter.initializer, nested, true);
    }
    if (node.body) walk(node.body, nested, true);
  }
  walk(source);
  return result;
}
analyze(JSON.parse(fs.readFileSync(0, 'utf8'))).then(result => {
  process.stdout.write(JSON.stringify(result));
}).catch(error => {
  process.stderr.write((error instanceof Error ? error.message : String(error)) + '\n');
  process.exitCode = 1;
});
