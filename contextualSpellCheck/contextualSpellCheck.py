import spacy
import torch
import editdistance
import datetime
import os

from spacy.tokens import Doc, Token, Span
from spacy.vocab import Vocab

from transformers import AutoModelWithLMHead, AutoTokenizer


class ContextualSpellCheck(object):
    """Class object for Out Of Vocabulary(OOV) corrections 
    """

    name = "contextual spellchecker"

    def __init__(self, vocab_path="", debug=False):
        if vocab_path == "":
            currentPath = os.path.dirname(__file__)
            vocab_path = os.path.join(currentPath, "data/vocab.txt")
        # self.nlp = spacy.load(
        #     "en_core_web_sm", disable=["tagger", "parser"]
        # )  # using default tokeniser with NER
        with open(vocab_path) as f:
            # if want to remove '[unusedXX]' from vocab
            # words = [line.rstrip() for line in f if not line.startswith('[unused')]
            words = [line.rstrip() for line in f]
        self.vocab = Vocab(strings=words)
        self.BertTokenizer = AutoTokenizer.from_pretrained("bert-base-cased")
        self.BertModel = AutoModelWithLMHead.from_pretrained("bert-base-cased")
        self.mask = self.BertTokenizer.mask_token
        self.debug = debug
        if not Doc.has_extension("contextual_spellCheck"):
            Doc.set_extension("contextual_spellCheck", default=True)
            Doc.set_extension("performed_spellCheck", default=False)

            # {originalToken-1:[suggestedToken-1,suggestedToken-2,..],
            #  originalToken-2:[...]}
            Doc.set_extension(
                "suggestions_spellCheck", getter=self.doc_suggestions_spellCheck
            )
            Doc.set_extension("outcome_spellCheck", default="")
            Doc.set_extension("score_spellCheck", default=None)

            Span.set_extension(
                "get_has_spellCheck", getter=self.span_require_spellCheck
            )
            Span.set_extension("score_spellCheck", getter=self.span_score_spellCheck)

            Token.set_extension(
                "get_require_spellCheck", getter=self.token_require_spellCheck
            )
            Token.set_extension(
                "get_suggestion_spellCheck", getter=self.token_suggestion_spellCheck
            )
            Token.set_extension("score_spellCheck", getter=self.token_score_spellCheck)

    def __call__(self, doc):
        """call function for the class. Used in spacy pipeline

        Arguments:
            doc {`Spacy.Doc`} -- Spacy Doc Object

        Returns:
            `Spacy.Doc` -- Updated doc object with custom extensions values
        """
        if self.debug:
            modelLodaded = datetime.datetime.now()
        misspellTokens, doc = self.misspellIdentify(doc)
        if self.debug:
            modelLoadTime = self.timeLog("Misspell identification: ", modelLodaded)
        if len(misspellTokens) > 0:
            candidate = self.candidateGenerator(doc, misspellTokens)
            if self.debug:
                modelLoadTime = self.timeLog("candidate Generator: ", modelLodaded)
            answer = self.candidateRanking(candidate)
            if self.debug:
                modelLoadTime = self.timeLog("candidate ranking: ", modelLodaded)
            updatedQuery = ""
            for i in doc:
                if i.i in [misspell.i for misspell in misspellTokens]:
                    updatedQuery += answer[i] + i.whitespace_
                else:
                    updatedQuery += i.text_with_ws

            if self.debug:
                print("Did you mean: ", updatedQuery)
            doc._.set("outcome_spellCheck", updatedQuery)
        return doc

    def check(self, query=""):
        """Complete pipeline for **testing purpose only**

        Keyword Arguments:
            query {str} -- query for which spell check model to run (default: {""})

        Returns:
            (str, `Doc`) -- returns updated query (if no oov words then "") and updated Doc Object
        """
        if type(query) != str and len(query) == 0:
            return ("Invalid query, expected non empty `str` but passed", query)

        nlp = spacy.load("en_core_web_sm", disable=["tagger", "parser"])
        doc = nlp(query)
        modelLodaded = datetime.datetime.now()
        misspellTokens, doc = self.misspellIdentify(doc)
        modelLoadTime = self.timeLog("Misspell identification: ", modelLodaded)
        updatedQuery = ""
        if len(misspellTokens) > 0:
            candidate = self.candidateGenerator(doc, misspellTokens)
            answer = self.candidateRanking(candidate)
            for i in doc:
                if i in misspellTokens:
                    updatedQuery += answer[i] + i.whitespace_
                else:
                    updatedQuery += i.text_with_ws

            print("Did you mean: ", updatedQuery)
            doc._.set("outcome_spellCheck", updatedQuery)
            # problem with below as it modifies the original object
        #             with doc.retokenize() as retokenizer:
        #                 print("Original text:",retokenizer.merge(doc[:]))
        return updatedQuery, doc

    def misspellIdentify(self, doc, query=""):
        """To identify misspelled words from the query

        At present, All the following criteria should be met for word to be misspelled
        1. Should not be in our vocab
        2. should not be a Person
        3. Should not be a number
        4. Should not be a url

        Arguments:
            doc {`Spacy.Doc`} -- Spacy doc object as input

        Keyword Arguments:
            query {str} -- not used now (default: {""})

        Returns:
            `tuple` -- returns `List[`Spacy.Token`]` and `Spacy.Doc`
        """

        # doc = self.nlp(query)
        misspell = []
        for token in doc:
            if (
                (token.text.lower() not in self.vocab)
                and (token.ent_type_ != "PERSON")
                and (not token.like_num)
                and (not token.like_email)
                and (not token.like_url)
            ):

                misspell.append(token)

        if self.debug:
            print(misspell)
        return (misspell, doc)

    def candidateGenerator(self, doc, misspellings, top_n=10):
        """Returns Candidates for misspell words

        This function is responsible for generating candidate list for misspell
        using BERT. The misspell is masked with a token (eg [MASK]) and the model tries to 
        predict `n` candidates for that mask. The `doc` is used to provide sentence (context) for the mask


        Arguments:
            doc {`Spacy.Doc`} -- Spacy Doc object, used to provide context to the model
            misspellings {List(`Spacy.Token`)} -- Contains List of `Token` object types from spacy to preserve meta information of the token 

        Keyword Arguments:
            top_n {int} -- # suggestions to be considered (default: {10})

        Returns:
            Dict{`Token`:List[{str}]} -- Eg of return type {misspell-1:['candidate-1','candidate-2', ...],
                            misspell-2:['candidate-1','candidate-2'. ...]}
        """
        response = {}
        score = {}

        for token in misspellings:
            updatedQuery = ""
            for i in doc:
                if i.i == token.i:
                    updatedQuery += self.mask + i.whitespace_
                else:
                    updatedQuery += i.text_with_ws
            if self.debug:
                print(
                    "For", "`" + token.text + "`", "updated query is:\n", updatedQuery
                )

            model_input = self.BertTokenizer.encode(updatedQuery, return_tensors="pt")
            mask_token_index = torch.where(
                model_input == self.BertTokenizer.mask_token_id
            )[1]
            token_logits = self.BertModel(model_input)[0]
            mask_token_logits = token_logits[0, mask_token_index, :]
            token_probability = torch.nn.functional.softmax(mask_token_logits, dim=1)
            top_n_score, top_n_tokens = torch.topk(token_probability, top_n, dim=1)
            top_n_tokens = top_n_tokens[0].tolist()
            top_n_score = top_n_score[0].tolist()
            if self.debug:
                print("top_n_tokens:", top_n_tokens)
                print("token_score: ", top_n_score)

            if token not in response:
                response[token] = [
                    self.BertTokenizer.decode([candidateWord])
                    for candidateWord in top_n_tokens
                ]
                score[token] = [
                    (
                        self.BertTokenizer.decode([top_n_tokens[i]]),
                        round(top_n_score[i], 5),
                    )
                    for i in range(top_n)
                ]

            # for candidate in top_5_tokens:
            # response[token].append(self.BertTokenizer.decode([candidate]))
            # print(updatedQuery.replace(self.mask, self.BertTokenizer.decode([candidate])))

            if self.debug:
                print("\nresponse: ", response, "\nscore: ", score)

        if len(misspellings) != 0:
            doc._.set("performed_spellCheck", True)
            doc._.set("score_spellCheck", score)

        return response

    def candidateRanking(self, misspellingsDict):
        """Ranking the candidates based on edit Distance

        At present using a library to calculate edit distance 
        between actual word and candidate words. Candidate word 
        for which edit distance is lowest is selected. If least 
        edit distance is same then word with higher probability 
        is selected by default

        Arguments:
            misspellingsDict {Dict{`Token`:List[{str}]}} -- 
            Orginal token is the key and candidate words are the values 

        Returns:
            Dict{`Token`:{str}} -- Eg of return type {misspell-1:'BEST-CANDIDATE'}
        """

        response = {}
        #         doc = self.nlp(query)
        for misspell in misspellingsDict:
            ## Init least_edit distance
            least_edit_dist = 100

            if self.debug:
                print("misspellingsDict[misspell]", misspellingsDict[misspell])
            for candidate in misspellingsDict[misspell]:
                edit_dist = editdistance.eval(misspell.text, candidate)
                if edit_dist < least_edit_dist:
                    least_edit_dist = edit_dist
                    response[misspell] = candidate

            if self.debug:
                print(response)
        return response

    def timeLog(self, fnName, relativeTime):
        """For time log

        Arguments:
            fnName {str} -- function name to print
            relativeTime {datetime} -- previous date time for subtraction

        Returns:
            datetime -- datetime of current logging
        """

        timeNow = datetime.datetime.now()
        print(fnName, "took: ", timeNow - relativeTime)
        return datetime.datetime.now()

    def token_require_spellCheck(self, token):
        """Getter for Token attributes. 

        Arguments:
            token {`Spacy.Token`} -- Token object for the value should be returned

        Returns:
            List -- If no suggestions: False else: True
        """
        return any(
            [
                token.i == suggestion.i
                for suggestion in token.doc._.suggestions_spellCheck.keys()
            ]
        )

    def token_suggestion_spellCheck(self, token):
        """Getter for Token attributes. 

        Arguments:
            token {`Spacy.Token`} -- Token object for the value should be returned

        Returns:
            List -- If no suggestions: [] else: List['suggestion-1','suggestion-1',...] 
        """
        for suggestion in token.doc._.suggestions_spellCheck.keys():
            if token.i == suggestion.i:
                return token.doc._.suggestions_spellCheck[token]
        return []

    def token_score_spellCheck(self, token):
        """Getter for Token attributes. 

        Arguments:
            token {`Spacy.Token`} -- Token object for the value should be returned

        Returns:
            List -- If no suggestions: [] else: List[('suggestion-1',score-1), ('suggestion-1',score-2), ...] 
        """
        if token.doc._.score_spellCheck is None:
            return []
        for suggestion in token.doc._.score_spellCheck.keys():
            if token.i == suggestion.i:
                return token.doc._.score_spellCheck[token]
        return []

    def span_score_spellCheck(self, span):
        """Getter for Span Object

        Arguments:
            span {`Spacy.Span`} -- Span object for which value should be returned

        Returns:
            List(Dict(`Token`:List(str,int))) -- for every token it will return (suggestion,score) eg: [{token-1: []}, {token-2: []}, {token-3: [('suggestion-1', score-1),]}] 
        """
        return [{token: self.token_score_spellCheck(token)} for token in span]

    def span_require_spellCheck(self, span):
        """Getter for Span Object

        Arguments:
            span {`Spacy.Span`} -- Span object for which value should be returned

        Returns:
            Boolean -- True if the span requires spellCheck
        """
        return any([self.token_require_spellCheck(token) for token in span])

    def doc_suggestions_spellCheck(self, doc):
        """Getter for Doc attribute

        Arguments:
            doc {`Spacy.Doc`} -- Doc object for which value should be returned

        Returns:
            Dict(`Spacy.Token`:List(str)) -- {misspell-1: ['suggestion-1', 'suggestion-2'...]}
        """
        response = {}
        if doc._.score_spellCheck is None:
            return response
        for token in doc._.score_spellCheck:
            if token not in response:
                response[token] = []
            for suggestion_score in doc._.score_spellCheck[token]:
                response[token].append(suggestion_score[0])
        return response


if __name__ == "__main__":
    print("Code running...")
    nlp = spacy.load("en_core_web_sm", disable=["tagger", "parser"])
    checker = ContextualSpellCheck(debug=False)
    nlp.add_pipe(checker)

    doc = nlp(u"Income was $9.4 milion compared to the prior year of $2.7 milion.")

    print("=" * 20, "Doc Extention Test", "=" * 20)
    print(doc._.outcome_spellCheck, "\n")

    print(doc._.contextual_spellCheck)
    print(doc._.performed_spellCheck)
    print(doc._.suggestions_spellCheck)
    print(doc._.score_spellCheck)
