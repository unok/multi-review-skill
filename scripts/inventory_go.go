// import の解決や対象パッケージの実行をせずに構文解析する。
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strconv"
	"strings"
)

type request struct{ File, Source string }
type declaration struct {
	Name      string `json:"name"`
	Value     string `json:"value,omitempty"`
	Signature string `json:"signature,omitempty"`
	Line      int    `json:"line"`
	EndLine   int    `json:"endLine"`
}
type literal struct {
	Kind  string `json:"kind"`
	Value string `json:"value"`
	Line  int    `json:"line"`
}
type result struct {
	Variables    []declaration `json:"variables"`
	Declarations []declaration `json:"declarations"`
	Literals     []literal     `json:"literals"`
}

func analyze(input request) (result, error) {
	output := result{Variables: []declaration{}, Declarations: []declaration{}, Literals: []literal{}}
	positions := token.NewFileSet()
	file, err := parser.ParseFile(positions, input.File, input.Source, parser.AllErrors|parser.SkipObjectResolution)
	if err != nil {
		return output, err
	}
	text := func(node ast.Node) string {
		return input.Source[positions.Position(node.Pos()).Offset:positions.Position(node.End()).Offset]
	}
	entry := func(node ast.Node, name string) declaration {
		return declaration{Name: name, Line: positions.PositionFor(node.Pos(), false).Line, EndLine: positions.PositionFor(node.End(), false).Line}
	}
	for _, node := range file.Decls {
		switch node := node.(type) {
		case *ast.GenDecl:
			for _, spec := range node.Specs {
				switch spec := spec.(type) {
				case *ast.ValueSpec:
					for index, name := range spec.Names {
						value := "［初期値なし］"
						if len(spec.Values) == len(spec.Names) {
							value = text(spec.Values[index])
						} else if len(spec.Values) > 0 {
							values := []string{}
							for _, expression := range spec.Values {
								values = append(values, text(expression))
							}
							value = strings.Join(values, ", ")
						} else if node.Tok == token.CONST {
							value = "［直前の定数式を省略継承］"
						}
						item := entry(spec, name.Name)
						item.Value = value
						output.Variables = append(output.Variables, item)
					}
				}
			}
		}
	}
	var literalError error
	var walk func(ast.Node, string)
	walk = func(root ast.Node, scope string) {
		ast.Inspect(root, func(node ast.Node) bool {
			switch node := node.(type) {
			case *ast.ImportSpec, *ast.FieldList, *ast.ArrayType, *ast.MapType,
				*ast.StructType, *ast.InterfaceType, *ast.FuncType, *ast.ChanType:
				return false
			case *ast.FuncDecl:
				name := node.Name.Name
				if node.Recv != nil {
					receiver := strings.TrimPrefix(text(node.Recv.List[0].Type), "*")
					if index := strings.Index(receiver, "["); index >= 0 {
						receiver = receiver[:index]
					}
					name = receiver + "." + name
				}
				item := entry(node, name)
				item.Signature = text(node)
				if node.Body != nil {
					item.Signature = strings.TrimSpace(input.Source[positions.Position(node.Pos()).Offset:positions.Position(node.Body.Pos()).Offset])
				}
				output.Declarations = append(output.Declarations, item)
				if node.Body != nil {
					walk(node.Body, name)
				}
				return false
			case *ast.TypeSpec:
				name := node.Name.Name
				if scope != "" {
					name = scope + "." + name
				}
				item := entry(node, name)
				item.Signature = "type " + text(node)
				output.Declarations = append(output.Declarations, item)
				return false
			case *ast.ValueSpec:
				for _, value := range node.Values {
					walk(value, scope)
				}
				return false
			case *ast.UnaryExpr:
				operand := node.X
				for {
					parenthesized, ok := operand.(*ast.ParenExpr)
					if !ok {
						break
					}
					operand = parenthesized.X
				}
				if value, ok := operand.(*ast.BasicLit); ok && (node.Op == token.ADD || node.Op == token.SUB) &&
					(value.Kind == token.INT || value.Kind == token.FLOAT) {
					prefix := ""
					if node.Op == token.SUB {
						prefix = "-"
					}
					output.Literals = append(output.Literals, literal{"number", prefix + value.Value, positions.PositionFor(node.Pos(), false).Line})
					return false
				}
			case *ast.BasicLit:
				kind, value := "number", node.Value
				switch node.Kind {
				case token.STRING:
					kind = "quoted-string"
					decoded, err := strconv.Unquote(value)
					if err != nil {
						literalError = err
						return false
					} // 文字列の構文自体はパーサーが検証済み。
					if strings.TrimSpace(decoded) == "" {
						return false
					}
					// Go の文字列は任意のバイト列なので、JSON の UTF-8 置換で値が混同されるのを防ぐ。
					value = strconv.Quote(decoded)
				case token.INT, token.FLOAT:
				default:
					return false // rune・虚数リテラルは文字列・実数の対象外。
				}
				output.Literals = append(output.Literals, literal{kind, value, positions.PositionFor(node.Pos(), false).Line})
			}
			return true
		})
	}
	walk(file, "")
	return output, literalError
}

func main() {
	var input request
	if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil {
		fail(err)
	}
	output, err := analyze(input)
	if err != nil {
		fail(err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		fail(err)
	}
}
func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
