from typing import TypedDict

# class ParentClass:
#     test = "This is a test attribute in the parent class."
#     name = "Zhengjie"

#     def __init__(self):
#         self.name = "Jie"
#         self.age = 50

#     def greet(self):
#         return f"Hello from {self.name}!"

#     def parent_method(self):
#         return "This is a method from the parent class."

#     @classmethod
#     def class_method(cls):
#         return f"This is a class method in {cls.test}."

#     @staticmethod
#     def static_method():
#         return "This is a static method in the parent class."


# class Parent2Class:
#     test2 = "This is a test attribute in the parent2 class."


# # 查看父类的结构
# # print("ParentClass attributes:", dir(ParentClass))


# class ChildClass(ParentClass, Parent2Class):
#     pass


# person = ChildClass()
# print(person.parent_method())  # 输出: Hello from Parent!
# print(person.test)
# print(person.name)
# print(person.class_method())


class Test(TypedDict):
    name: str
    age: int


test = Test(name="Alice", age=30)

print(test["age"])
print(test["name"])

person = {"name": "Alice", "age": 30}

print(person["age"])
print(person.get("name"))
