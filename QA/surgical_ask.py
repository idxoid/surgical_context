import argparse
import os

import ollama
from neo4j import GraphDatabase

# --- CONFIG ---
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password"

class SurgicalContextAI:
    def __init__(self):
        self.driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    def read_snippet(self, path, line_range):
        """Читает код, учитывая, что в Neo4j range — это [start, end]"""
        if not path or not os.path.exists(path):
            return f"# Error: File not found at {path}"
        try:
            with open(path, encoding='utf-8') as f:
                lines = f.readlines()
                start, end = line_range
                # range [4, 6] в Python индексах это 3:6 (строки 4, 5, 6)
                return "".join(lines[start-1:end])
        except Exception as e:
            return f"# Error reading file: {e}"

    def get_context(self, symbol_name):
        query = """
        MATCH (s:Symbol)
        WHERE s.name =~ ('(?i)' + $name)
        OPTIONAL MATCH (s)-[:CALLS]->(dep:Symbol)
        RETURN s.name as name, s.file_path as path, s.range as range,
               collect({name: dep.name, path: dep.file_path, range: dep.range}) as deps
        """
        with self.driver.session() as session:
            result = session.run(query, name=symbol_name).single()
            if not result: return None

            # Читаем целевую функцию
            code = self.read_snippet(result['path'], result['range'])
            context = f"--- TARGET: {result['name']} ({result['path']}) ---\n{code}\n"

            # Читаем зависимости (если есть)
            if result['deps']:
                context += "\n--- DEPENDENCIES ---\n"
                for dep in result['deps']:
                    if dep['path'] and dep['range']:
                        dep_code = self.read_snippet(dep['path'], dep['range'])
                        context += f"# From {dep['path']} ({dep['name']}):\n{dep_code}\n"
            print(context)
            return context

    def ask(self, symbol_name, question):
        context = self.get_context(symbol_name)
        print("\n--- ОТВЕТ AI ---")
        if not context:
            return f"❌ Символ '{symbol_name}' не найден в графе."

        print(f"✅ Контекст для '{symbol_name}' успешно извлечен из файлов.")
        
        response = ollama.chat(model="llama3", messages=[
            {'role': 'system', 'content': "You are a Surgical Code Assistant. Use ONLY provided context. Be concise. Respond in Russian."},
            {'role': 'user', 'content': f"Context:\n{context}\n\nQuestion: {question}"}
        ])
        return response['message']['content']

    def close(self):
        self.driver.close()

def main():
    parser = argparse.ArgumentParser(description="Surgical Context AI: Умный помощник по коду")
    parser.add_argument("symbol", help="Имя функции или класса для анализа")
    parser.add_argument("-q", "--question", default="Что делает этот код?", help="Ваш вопрос к ИИ")
    parser.add_argument("-m", "--model", default="llama3", help="Модель Ollama (по умолчанию llama3)")
    
    args = parser.parse_args()

    ai = SurgicalContextAI()
    try:
        print(f"\n🚀 Запрос к символу: {args.symbol}")
        print(f"❓ Вопрос: {args.question}")
        print("-" * 30)
        
        answer = ai.ask(args.symbol, args.question)
        
        print("\n--- ОТВЕТ AI ---")
        print(answer)
    except KeyboardInterrupt:
        print("\n👋 Выход...")
    finally:
        ai.close()

if __name__ == "__main__":
    main()