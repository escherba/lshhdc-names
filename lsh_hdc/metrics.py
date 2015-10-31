"""

Motivation
----------

The motivation behind the given re-implementation of some clustering metrics is
to avoid the high memory usage of equivalent methods in Scikit-Learn.  Using
sparse dictionary maps avoids storing co-incidence matrices in memory leading to
more acceptable performance in multiprocessing environment or on very large data
sets.

A side goal was to investigate different association metrics with the aim of
applying them to evaluation of clusterings in semi-supervied learning and
feature selection in supervised learning.

Finally, I was interested in the applicability of different association metrics
to different types of experimental design. At present, there seems to be both
(1) a lot of confusion about the appropriateness of different metrics, and (2)
relatively little attention paid to the type of experimental design used. I
believe that, at least partially, (1) stems from (2), and that different types
of experiments call for different categories of metrics.

Contingency Tables and Experimental Design
------------------------------------------

Consider studies that deal with two variables whose respective realizations can
be represented as rows and columns in a table.  Roughly adhering to the
terminology proposed in [1]_, we distinguish four types of experimental design
all involving contingency tables.

* Sampling in a Model O study is entirely random. Neither columns nor rows, nor
the grand total are fixed.
* Under Model I, random sampling occurs both row- and column-wise, but the grand
total is fixed.
* Under Model II, one side (either row or column totals) is fixed.
* Under Model III, both rows and column totals are fixed.

Model O is rarely employed in practice because the researchers almost always
have a certain number of samples they are interesting in drawing before they
begin the experiment. An example of a Model O study would be astronomy research
that tests a hypothesis about a generalizable property such as dark matter
content by looking at all galaxies in the Local Group, and the researchers
obviously don't get to choose ahead of time how many galaxies there are near
ours.

Model I and Model II studies are the most common and usually the most confusion
arises from mistaking one for the other. In psychology, interrater agreement is
an example of Model I approach. A replication study, if performed by the
original author, is a Model I study, but if performed by another group of
researchers, becomes a Model II study.

Fisher's classic example of tea-tasting is an example of Model III study [2]_.
The key differnce from a Model II study here is that the subject was asked to
select 4 cups prepared by one method, not any number of cups. The subject was
not free to say, for example, that none of the cups were prepared by adding milk
first. The hypergeometric distribution used in the subsequent Fisher's exact
test shares the assumption of the experiment that both row and column counts are
fixed.

Relevance of Experimental Design to the Choice of Association Metric
--------------------------------------------------------------------

Given the types of experimental design listed above, some metrics seem to be
more appropriate than others. For example, two-way correlation coefficients
appear to be inappropriate for Model II studies where their respective regression
components seem more suited to judging association.

Additionally, if there is implied causality relationship, one-sided measures
might be preferred. For example, when performing feature selection, it seems
logical to conclude that the presence of features should bee seen as influencing
the class label, not the other way around.

Using Monte-Carlo methods, it should be possible to test the validity of the
above two propositions as well as to visualize the effect of the assumptions
made.

References
----------

.. [1] `Sokal, R. R., & Rohlf, F. J. (2012). Biometry (4th edn). pp. 742-744.
       <http://www.amazon.com/dp/0716786044>`_

.. [2] Wikipedia entry on Fisher's "Lady Tasting Tea" experiment
       <https://en.wikipedia.org/wiki/Lady_tasting_tea>`_

"""

import numpy as np
from math import log as logn, sqrt, copysign
from collections import Mapping, Set, namedtuple
from itertools import izip
from operator import itemgetter
from sklearn.metrics.ranking import roc_curve, auc
from pymaptools.iter import aggregate_tuples
from pymaptools.containers import TableOfCounts
from lsh_hdc.fixes import bincount
from lsh_hdc.expected_mutual_info_fast import expected_mutual_information


class pretty_repr(type):
    def _repr_pretty_(self, p, cycle):
        p.text(repr(self))


def nchoose2(n):
    """Binomial coefficient for k=2

    Scipy has ``scipy.special.binom`` and ``scipy.misc.comb``, however on
    individaul (non-vectorized) ops used in memory-constratined stream
    computation, a simple definition below is faster. It is possible to get the
    best of both worlds by writing a generator that returns NumPy arrays of
    limited size and then calling a vectorized n-choose-2 function on those,
    however the current way is fast enough for computing coincidence matrices
    (turns out memory was the bottleneck, not raw computation speed).
    """
    return (n * (n - 1)) >> 1


def _div(numer, denom):
    """Divide without raising zero division error or losing decimal part
    """
    if denom == 0:
        if numer == 0:
            return np.nan
        elif numer > 0:
            return np.PINF
        else:
            return np.NINF
    return float(numer) / denom


def jaccard_similarity(set1, set2):
    """Return Jaccard similarity between two sets

    :param set1: set 1
    :param set2: set 2
    :returns: Jaccard similarity of two sets
    :rtype: float
    """
    cm = ConfusionMatrix2.from_sets(set1, set2)
    return cm.jaccard_coeff()


def centropy(counts):
    """Returns centropy of an iterable of counts

    Assumes every entry in the list belongs to a different class.

    The parameter `counts` is expected to be an list or tuple-like iterable.
    For convenience, it can also be a dict/mapping type, in which case its
    values will be used to calculate centropy.

    The centropy is calculated using natural base, which may not be what you
    want, so caveat emptor.

    """
    if isinstance(counts, Mapping):
        counts = counts.itervalues()

    n = 0
    sum_c_logn_c = 0.0
    for c in counts:
        if c != 0:
            n += c
            sum_c_logn_c += c * logn(c)
    return 0.0 if n == 0 else n * logn(n) - sum_c_logn_c


def lentropy(labels):
    """Calculates the entropy for a labeling."""
    if len(labels) == 0:
        return 1.0
    label_idx = np.unique(labels, return_inverse=True)[1]
    pi = bincount(label_idx).astype(np.float)
    pi = pi[pi > 0]
    pi_sum = np.sum(pi)
    # log(a / b) should be calculated as log(a) - log(b) for
    # possible loss of precision
    return -np.sum((pi / pi_sum) * (np.log(pi) - logn(pi_sum)))


def ratio2weights(ratio):
    """Numerically accurate conversion of ratio of two weights to weights
    """
    if ratio <= 1.0:
        lweight = ratio / (1.0 + ratio)
    else:
        lweight = 1.0 / (1.0 + 1.0 / ratio)
    return lweight, 1.0 - lweight


def geometric_mean(x, y):
    """Geometric mean of two numbers. Returns a float

    Although geometric mean is defined for negative numbers, Scipy function
    doesn't allow it... sigh
    """
    prod = x * y
    if prod < 0.0:
        raise ValueError("x and y have different signs")
    return copysign(1, x) * sqrt(prod)


def geometric_mean_weighted(x, y, ratio=1.0):
    """Geometric mean of two numbers with a weight ratio. Returns a float

    >>> geometric_mean_weighted(1, 4, ratio=1.0)
    2.0
    >>> geometric_mean_weighted(1, 4, ratio=0.0)
    1.0
    >>> geometric_mean_weighted(1, 4, ratio=float('inf'))
    4.0
    """
    lweight, rweight = ratio2weights(ratio)
    lsign = copysign(1, x)
    rsign = copysign(1, y)
    if lsign != rsign and x != y:
        raise ValueError("x and y have different signs")
    return lsign * (abs(x) ** rweight) * (abs(y) ** lweight)


def harmonic_mean(x, y):
    """Harmonic mean of two numbers. Returns a float
    """
    return float(x) if x == y else 2.0 * (x * y) / (x + y)


def harmonic_mean_weighted(x, y, ratio=1.0):
    """Harmonic mean of two numbers with a weight ratio. Returns a float

    >>> harmonic_mean_weighted(1, 3, ratio=1.0)
    1.5
    >>> harmonic_mean_weighted(1, 3, ratio=0.0)
    1.0
    >>> harmonic_mean_weighted(1, 3, ratio=float('inf'))
    3.0
    """
    lweight, rweight = ratio2weights(ratio)
    return float(x) if x == y else (x * y) / (lweight * x + rweight * y)


class ContingencyTable(TableOfCounts):

    # TODO: subclass pandas.DataFrame instead

    def chisq_score(self):
        """Pearson's chi-square statistic
        """
        N = float(self.grand_total)
        score = 0.0
        for rm, cm, observed in self.iter_cells_with_margins():
            numer = rm * cm
            if numer != 0:
                expected = numer / N
                score += (observed - expected) ** 2 / expected
        return score

    def _entropies(self):
        """Return H_C, H_K, and mutual information

        Not normalized by N
        """
        H_C = centropy(self.row_totals)
        H_K = centropy(self.col_totals)
        H_actual = centropy(self.iter_cells())
        H_expected = H_C + H_K
        I_CK = H_expected - H_actual
        return H_C, H_K, I_CK

    def vi_distance(self, base=2.0):
        """Variation of Information distance

        Normalized to log base 2
        """
        H_C, H_K, I_CK = self._entropies()
        VI_CK = (H_C - I_CK) + (H_K - I_CK)
        return VI_CK / (logn(base) * self.grand_total)

    def split_join_distance(self):
        """Projection distance between partitions

        Used in graph commmunity analysis. Originally defined by van Dogen.
        Example given in [1]:

        >>> p1 = [{1, 2, 3, 4}, {5, 6, 7}, {8, 9, 10, 11, 12}]
        >>> p2 = [{2, 4, 6, 8, 10}, {3, 9, 12}, {1, 5, 7}, {11}]
        >>> cm = ClusteringMetrics.from_partitions(p1, p2)
        >>> cm.split_join_distance()
        11

        References
        ----------

        [1] Dongen, S. V. (2000). Performance criteria for graph clustering and
        Markov cluster experiments. Information Systems [INS], (R 0012), 1-36.

        """
        pa_B = sum(max(x) for x in self.iter_rows())
        pb_A = sum(max(x) for x in self.iter_cols())
        return 2 * self.grand_total - pa_B - pb_A

    def talburt_wang_index(self):
        """Talburt-Wang index of similarity of two partitionings

        Example
        -------

        >>> ltrue = [ 1,  1,  1,  2,  2,  2,  2,  3,  3,  4]
        >>> lpred = [43, 56, 56,  5, 36, 36, 36, 74, 74, 66]
        >>> cm = ContingencyTable.from_labels(ltrue, lpred)
        >>> round(cm.talburt_wang_index(), 3)
        0.816

        >>> clusters = [{1, 1}, {1, 1, 1, 1}, {2, 3}, {2, 2, 3, 3},
        ...             {3, 3, 4}, {3, 4, 4, 4, 4, 4, 4, 4, 4, 4}]
        >>> cm = ContingencyTable.from_clusters(clusters)
        >>> round(cm.talburt_wang_index(), 2)
        0.49

        References
        ----------

        .. [1] Talburt, J., Wang, R., Hess, K., & Kuo, E. (2007). An algebraic
            approach to data quality metrics for entity resolution over large
            datasets.  Information quality management: Theory and applications,
            1-22.
        """
        V_card = 0
        A_card = len(list(self.iter_row_totals()))
        B_card = len(list(self.iter_col_totals()))
        for row in self.iter_rows():
            V_card += len(list(row))
        prod = A_card * B_card
        return np.nan if prod == 0 else sqrt(prod) / V_card

    def mutual_info_score(self):
        """Mutual Information Score

        Mutual Information (divided by N).

        The metric is equal to the Kullback-Leibler divergence of the joint
        distribution with the product distribution of the marginals
        """
        _, _, I_CK = self._entropies()
        return I_CK / self.grand_total

    def g_score(self):
        """Returns G-statistic for RxC contingency table

        This method does not perform any corrections to this statistic (e.g.
        Williams', Yates' corrections).

        The statistic is equivalent to the negative of Mutual Information times
        two.  Mututal Information on a contingency table is defined as the
        difference between the information in the table and the information in
        an independent table with the same margins.  For application of mutual
        information (in the form of G-score) to search for collocated words in
        NLP, see [1]_ and [2]_.

        References
        ----------

        .. [1] `Dunning, T. (1993). Accurate methods for the statistics of
               surprise and coincidence. Computational linguistics, 19(1), 61-74.
               <http://dl.acm.org/citation.cfm?id=972454>`_

        .. [2] `Ted Dunning's personal blog entry and the discussion under it.
               <http://tdunning.blogspot.com/2008/03/surprise-and-coincidence.html>`_

        """
        _, _, I_CK = self._entropies()
        return 2.0 * I_CK

    def entropy_metrics(self):
        """Calculate three entropy-based metrics used for clustering evaluation

        The metrics are: Homogeneity, Completeness, and V-measure

        The V-measure metric is also known as Normalized Mutual Information
        (NMI), and is defined here as the harmonic mean of Homogeneity and
        Completeness.  Homogeneity and Completeness are duals of each other and
        can be thought of as squared regression coefficients of a given
        clustering vs true labels (homogeneity) and of the dual problem of true
        labels vs given clustering (completeness). Because of the dual property,
        in a symmetric matrix, all three scores are the same.

        This code is replaces an equivalent function in Scikit-Learn known as
        `homogeneity_completeness_v_measure` (the Scikit-Learn version takes up
        O(n^2) space because it stores data in a dense NumPy array) while the
        given version is subquadratic because of sparse underlying storage.

        Note that the entropy variables as used directly in the code below are
        improperly defined because they ought to be divided by N (the grand
        total for the contigency table). However, the N variable cancels out
        during normalization.

        """
        # ensure non-negative values by taking max of 0 and given value
        H_C, H_K, I_CK = self._entropies()
        h = 1.0 if H_C == 0.0 else max(0.0, I_CK / H_C)
        c = 1.0 if H_K == 0.0 else max(0.0, I_CK / H_K)
        rsquare = harmonic_mean(h, c)
        return h, c, rsquare


class ClusteringMetrics(ContingencyTable):

    """Provides external clustering evaluation metrics

    A subclass of ContingencyTable that builds a pairwise co-association matrix
    for clustering comparisons.
    """

    def __init__(self, *args, **kwargs):
        super(ClusteringMetrics, self).__init__(*args, **kwargs)
        self._coassoc_ = None

    @property
    def coassoc_(self):
        """Compute a confusion matrix describing pairs from two partitionings

        Given two partitionings A and B and a co-occurence matrix of point pairs,

        TP - count of pairs found in the same partition in both A and B
        FP - count of pairs found in the same partition in A but not in B
        FN - count of pairs found in the same partition in B but not in A
        TN - count of pairs in different partitions in both A and B

        Note that although the resulting confusion matrix has the form of a
        correlation table for two binary variables, it is not symmetric if the
        original partitionings are not symmetric.

        """
        coassoc = self._coassoc_
        if coassoc is None:
            actual_positives = sum(nchoose2(b) for b in self.iter_row_totals())
            called_positives = sum(nchoose2(a) for a in self.iter_col_totals())
            TP = sum(nchoose2(cell) for cell in self.iter_cells())
            FN = actual_positives - TP
            FP = called_positives - TP
            TN = nchoose2(self.grand_total) - TP - FP - FN
            coassoc = self._coassoc_ = ConfusionMatrix2.from_ccw(TP, FP, TN, FN)
        return coassoc

    def get_score(self, scoring_method, *args, **kwargs):
        """Convenience method that looks up and runs a scoring method
        """
        try:
            method = getattr(self, scoring_method)
        except AttributeError:
            method = getattr(self.coassoc_, scoring_method)
        return method(*args, **kwargs)

    def adjusted_jaccard_coeff(self):
        """Jaccard similarity coefficient with correction for chance

        Uses Taylor series-based correction described in [1]

        .. [1] `Albatineh, A. N., & Niewiadomska-Bugaj, M. (2011). Correcting
           Jaccard and other similarity indices for chance agreement in cluster
           analysis. Advances in Data Analysis and Classification, 5(3), 179-200.
           <https://doi.org/10.1007/s11634-011-0090-y>`_
        """
        n = self.grand_total
        coassoc = self.coassoc_
        P = 2 * (coassoc.TP + coassoc.FN)
        Q = 2 * (coassoc.TP + coassoc.FP)
        PnQn_over_nsq = ((P + n) * (Q + n)) / float(n ** 2)
        numer = PnQn_over_nsq - n
        denom = (P + Q + n) - PnQn_over_nsq
        expected = numer / denom
        coeff = coassoc.jaccard_coeff()
        adjusted = (coeff - expected) / (1.0 - expected)
        return adjusted

    def adjusted_sokal_sneath_coeff(self):
        """Sokal-Sneath similarity coefficient with correction for chance

        Uses Taylor series-based correction described in [1]

        .. [1] `Albatineh, A. N., & Niewiadomska-Bugaj, M. (2011). Correcting
           Jaccard and other similarity indices for chance agreement in cluster
           analysis. Advances in Data Analysis and Classification, 5(3), 179-200.
           <https://doi.org/10.1007/s11634-011-0090-y>`_
        """
        n = self.grand_total
        coassoc = self.coassoc_
        P = 2 * (coassoc.TP + coassoc.FN)
        Q = 2 * (coassoc.TP + coassoc.FP)
        PnQn_over_nsq = (P + n) * (Q + n) / float(n ** 2)
        numer = PnQn_over_nsq - n
        denom = 2 * (P + Q + 2 * n) - n - (3 * PnQn_over_nsq)
        expected = numer / denom
        coeff = coassoc.sokal_sneath_coeff()
        adjusted = (coeff - expected) / (1.0 - expected)
        return adjusted

    def adjusted_rogers_tanimoto_coeff(self):
        """Rogers-Tanimoto similarity coefficient with correction for chance

        Uses Taylor series-based correction described in [1]

        .. [1] `Albatineh, A. N., & Niewiadomska-Bugaj, M. (2011). Correcting
           Jaccard and other similarity indices for chance agreement in cluster
           analysis. Advances in Data Analysis and Classification, 5(3), 179-200.
           <https://doi.org/10.1007/s11634-011-0090-y>`_
        """
        n = self.grand_total
        coassoc = self.coassoc_
        P = 2 * (coassoc.TP + coassoc.FN)
        Q = 2 * (coassoc.TP + coassoc.FP)
        PnQn_over_nsq = (P + n) * (Q + n) / float(n ** 2)
        nn1 = n * (n - 1)
        PQ2n = P + Q + 2 * n
        numer = 2 * PnQn_over_nsq + nn1 - PQ2n
        denom = PQ2n + nn1 - 2 * PnQn_over_nsq
        expected = numer / denom
        coeff = coassoc.rogers_tanimoto_coeff()
        adjusted = (coeff - expected) / (1.0 - expected)
        return adjusted

    def adjusted_gower_legendre_coeff(self):
        """Gower-Legendre similarity coefficient with correction for chance

        Uses Taylor series-based correction described in [1]

        .. [1] `Albatineh, A. N., & Niewiadomska-Bugaj, M. (2011). Correcting
           Jaccard and other similarity indices for chance agreement in cluster
           analysis. Advances in Data Analysis and Classification, 5(3), 179-200.
           <https://doi.org/10.1007/s11634-011-0090-y>`_
        """
        n = self.grand_total
        coassoc = self.coassoc_
        P = 2 * (coassoc.TP + coassoc.FN)
        Q = 2 * (coassoc.TP + coassoc.FP)
        PnQn_over_nsq = (P + n) * (Q + n) / float(n ** 2)
        nn1 = n * (n - 1)
        PQ2n = P + Q + 2 * n
        numer = 2 * PnQn_over_nsq + nn1 - PQ2n
        denom = PnQn_over_nsq + nn1 - 0.5 * PQ2n
        expected = numer / denom
        coeff = coassoc.gower_legendre_coeff()
        adjusted = (coeff - expected) / (1.0 - expected)
        return adjusted


confmat2_type = namedtuple("ConfusionMatrix2", "TP FP TN FN")


class ConfusionMatrix2(ContingencyTable):
    """A confusion matrix (2x2 contingency table)

    For a binary variable (where one is measuring either presence vs absence of
    a particular feature), a confusion matrix where the ground truth levels are
    rows looks like:

        TP  FN
        FP  TN

    For a nominal variable, the negative class becomes a distinct label, and
    TP/FP/FN/TN terminology does not apply, although the algorithms should work
    the same way (with the obvious distinction that different assumptions will
    be made).
    """

    __metaclass__ = pretty_repr

    def __repr__(self):
        return repr(self.to_array())

    def __init__(self, TP, FN, FP, TN):
        super(ConfusionMatrix2, self).__init__(rows=((TP, FN), (FP, TN)))

    @classmethod
    def from_rows(cls, rows):
        return super(ConfusionMatrix2, cls)(rows=rows)

    from_array = from_rows

    @classmethod
    def from_sets(cls, set1, set2, universe_size=None):
        """Create a confusion matrix for comparison of two sets

        Accepts an optional universe_size parameter which allows us to take into
        account TN class and use probability-based similarity metrics.  Most of
        the time, however, set comparisons are performed ignoring this parameter
        and relying instead on non-probabilistic indices such as Jaccard's or
        Dice.
        """
        if not isinstance(set1, Set):
            set1 = set(set1)
        if not isinstance(set2, Set):
            set2 = set(set2)
        TP = len(set1 & set2)
        FP = len(set2) - TP
        FN = len(set1) - TP
        if universe_size is None:
            TN = 0
        else:
            TN = universe_size - TP - FP - FN
            if TN < 0:
                raise ValueError(
                    "universe_size must be at least as large as set union")
        return cls(TP, FN, FP, TN)

    def to_array(self):
        return np.array(self.to_rows())

    def to_rows(self):
        return ((self.TP, self.FN), (self.FP, self.TN))

    @classmethod
    def from_cols(cls, cols):
        return super(ConfusionMatrix2, cls)(cols=cols)

    @classmethod
    def from_random_counts(cls, low=0, high=100):
        """Return a matrix instance initialized with random values
        """
        return cls(*np.random.randint(low=low, high=high, size=(4,)))

    @classmethod
    def from_ccw(cls, TP, FP, TN, FN):
        return cls(TP, FN, FP, TN)

    def to_ccw(self):
        return confmat2_type(TP=self.TP, FP=self.FP, TN=self.TN, FN=self.FN)

    def get_score(self, scoring_method, *args, **kwargs):
        """Convenience method that looks up and runs a scoring method
        """
        method = getattr(self, scoring_method)
        return method(*args, **kwargs)

    @property
    def TP(self):
        return self.rows[0][0]

    @property
    def FN(self):
        return self.rows[0][1]

    @property
    def FP(self):
        return self.rows[1][0]

    @property
    def TN(self):
        return self.rows[1][1]

    def ACC(self):
        """Accuracy (Simple Matching Coefficient, Rand Index)
        """
        return _div(self.TP + self.TN, self.grand_total)

    def PPV(self):
        """Precision (Positive Predictive Value)
        """
        return _div(self.TP, self.TP + self.FP)

    def NPV(self):
        """Negative predictive value
        """
        return _div(self.TN, self.TN + self.FN)

    def TPR(self):
        """Recall (Sensitivity)

        Also known as hit rate
        """
        return _div(self.TP, self.TP + self.FN)

    def FPR(self):
        """Fallout (False Positive Rate)

        Synonyms: fallout, false alarm rate
        """
        return _div(self.FP, self.TN + self.FP)

    def TNR(self):
        """Specificity (True Negative Rate)
        """
        return _div(self.TN, self.FP + self.TN)

    def FNR(self):
        """Miss Rate (False Negative Rate)
        """
        return _div(self.FN, self.TP + self.FN)

    def FDR(self):
        """False discovery rate
        """
        return _div(self.FP, self.TP + self.FP)

    def FOR(self):
        """False omission rate
        """
        return _div(self.FN, self.TN + self.FN)

    def PLL(self):
        """Positive likelihood ratio
        """
        return _div(self.TPR(), self.FPR())

    def NLL(self):
        """Negative likelihood ratio
        """
        return _div(self.FNR(), self.TNR())

    def DOR(self):
        """Diagnostics odds ratio

        Equal to PLL / NLL
        """
        return _div(self.TP * self.TN, self.FP * self.FN)

    def fscore(self, beta=1.0):
        """F-score

        As beta tends to infinity, F-score will approach recall.  As beta tends
        to zero, F-score will approach precision. For a similarity coefficient
        see dice_coeff.
        """
        return harmonic_mean_weighted(self.precision(), self.recall(), beta ** 2)

    def dice_coeff(self):
        """Dice similarity coefficient (Nei-Li coefficient)

        This is the same as F1-score, but calculated slightly differently here.
        Note that Dice can be zero if total number of positives is zero, but
        F-score is undefined in that case (because recall is undefined).

        See Also
        --------
        jaccard_coeff, ochiai_coeff

        """
        a = self.TP
        return _div(2 * a, 2 * a + self.FN + self.FP)

    def rogers_tanimoto_coeff(self):
        """Rogers-Tanimoto similarity coefficient

        Like Gower-Legendre but upweighs 'b + c'
        """
        a_plus_d = self.TP + self.TN
        return _div(a_plus_d, a_plus_d + 2 * (self.FN + self.FP))

    def gower_legendre_coeff(self):
        """Gower-Legendre similarity coefficient

        Like Rogers-Tanimoto but downweighs 'b + c'
        """
        a_plus_d = self.TP + self.TN
        return _div(a_plus_d, a_plus_d + 0.5 * (self.FN + self.FP))

    def jaccard_coeff(self):
        """Jaccard similarity coefficient

        Other metrics from the same family: dice_coeff, ochiai_coeff
        """
        return _div(self.TP, self.TP + self.FP + self.FN)

    def ochiai_coeff(self):
        """Ochiai similarity coefficient (Fowlkes-Mallows, Cosine similarity)

        Other metrics from the same family: jaccard_coeff, dice_coeff

        This similarity index has an interpretation that it is the geometric
        mean of the conditional probability of an element (in the case of
        pairwise clustering comparison, a pair of elements) belonging to the
        same cluster given that they belong to the same class [0].

        References
        ----------
        [0] Ramirez, E. H., Brena, R., Magatti, D., & Stella, F. (2012). Topic
        model validation. Neurocomputing, 76(1), 125-133.
        http://dx.doi.org/10.1016/j.neucom.2011.04.032
        """
        a, b, c = self.TP, self.FN, self.FP
        return _div(a, sqrt((a + b) * (a + c)))

    def sokal_sneath_coeff(self):
        """Sokal and Sneath similarity index

        In a 2x2 matrix

            a b
            c d,

        Dice places more weight on 'a' component, Jaccard places equal weight on
        'a' and 'b + c', while Sokal and Sneath places more weight on 'b + c'.
        """
        a = self.TP
        return _div(a, a + 2 * (self.FN + self.FP))

    def prevalence_index(self):
        """Prevalence

        In interrater agreement studies, prevalence is high when the proportion
        of agreements on the positive classification differs from that of the
        negative classification.  Example of a confusion matrix with high
        prevalence:

            3   27
            28  132

        In the example given, both raters agree that there are very few positive
        examples relative to the number of negatives. In other word, the
        negative rating is very prevalent.
        """
        return _div(abs(self.TP - self.TN), self.grand_total)

    def bias_index(self):
        """Bias

        In interrater agreement studies, bias is the extent to which the raters
        disagree on the positive-negative ratio of the binary variable studied.
        Example of a confusion matrix with high bias:

            17  14
            78  81

        Note that rater whose judgement is represented by rows (A) believes
        there are a lot more negative examples than positive ones while the
        rater whose judgement is represented by columns (B) thinks the number of
        positives is roughly equal to the number of negatives. In other words,
        the rater A appears to be negatively biased.
        """
        return _div(abs(self.FN - self.FP), self.grand_total)

    def informedness(self):
        """Informedness (Recall corrected for chance)

        Alternative formulations:

            Informedness = Sensitivity + Specificity - 1.0
                         = TPR - FPR

        Synonyms: True Skill Score, Hannssen-Kuiper Score, Attributable Risk.
        """
        p1, q1 = self.row_totals.values()
        return _div(self.covar(), p1 * q1)

    def markedness(self):
        """Markedness (Precision corrected for chance)

        Alternative formulation:

            Markedness = PPV + NPV - 1.0

        """
        p2, q2 = self.col_totals.values()
        return _div(self.covar(), p2 * q2)

    def kappa0(self):
        """One-sided component of Kappa, Matthews, and Loevinger indices

        Roughly corresponds to precision
        """
        _, q1 = self.row_totals.values()
        p2, _ = self.col_totals.values()
        return _div(self.covar(), p2 * q1)

    def kappa1(self):
        """One-sided component of Kappa, Matthews, and Loevinger indices

        Roughly corresponds to recall
        """
        p1, _ = self.row_totals.values()
        _, q2 = self.col_totals.values()
        return _div(self.covar(), p1 * q2)

    def loevinger_coeff(self):
        """Loevinger two-sided coefficient of homogeneity

        Given a clustering (numbers correspond to class labels, inner groups to
        clusters) with perfect homogeneity but imperfect completeness, Loevinger
        coefficient returns a perfect score on the corresponding pairwise
        co-association matrix::

        >>> clusters = [[0, 0], [0, 0, 0, 0], [1, 1, 1, 1]]
        >>> cm = ClusteringMetrics.from_clusters(clusters)
        >>> cm.coassoc_.loevinger_coeff()
        1.0

        At the same time, kappa and matthews coefficients are 0.63 and 0.68,
        respectively. Being symmetrically defined, Loevinger coefficient will
        also return a perfect score in the dual (opposite) situation::

        >>> clusters = [[0, 2, 2, 0, 0, 0], [1, 1, 1, 1]]
        >>> cm = ClusteringMetrics.from_clusters(clusters)
        >>> cm.coassoc_.loevinger_coeff()
        1.0

        Loevinger's coefficient has a unique property: all relevant two-way
        correlation coefficients on a 2x2 table (including Kappa and Matthews'
        Correlation Coefficient) become Loevinger's coefficient after
        normalization by maximum value [1]_.

        References
        ----------

        .. [1] `Warrens, M. J. (2008). On association coefficients for 2x2
               tables and properties that do not depend on the marginal
               distributions.  Psychometrika, 73(4), 777-789.
               <https://doi.org/10.1007/s11336-008-9070-3>`_

        """
        p1, q1 = self.row_totals.values()
        p2, q2 = self.col_totals.values()
        return _div(self.covar(), min(p1 * q2, p2 * q1))

    def kappa(self):
        """Cohen's Kappa (Interrater Agreement, Adjusted Rand Index)

        Kappa index comes from psychology and was originally introduced to
        measure interrater agreement. It has also been used in replication
        evaluation [1]_, reliability studies [2]_, clustering evaluation [3]_,
        and feature selection [4]_.

        Kappa can be derived by correcting Accuracy (Simple Matching
        Coefficient, Rand Index) for chance. Tbe general formula for chance
        correction of an association measure ``M`` is:

                      M - E(M)
            M_adj = ------------ ,
                    M_max - E(M)

        where ``M_max`` is the maximum value a measure ``M`` can achieve, and
        ``E(M)`` is the expected value of ``M`` under statistical independence
        given fixed table margins.

        Kappa can be decomposed into a pair of components (regression
        coefficients for a problem and its dual) of which it is a harmonic mean:

            k1 = cov / (p1 * q2)       # recall-like
            k0 = cov / (p2 * q1)       # precision-like

        It is interesting to note that if one takes a geometric mean of the
        above two components, one obtains Matthews' Correlation Coefficient.
        The latter is also obtained from a geometric mean of informedness and
        markedness (which are similar to, but not the same, as k1 and k0).
        Unlike informedness and markedness, k0 and k1 don't have a lower bound.
        For that reason, when characterizing one-way dependence in a 2x2
        confusion matrix, it is arguably better to use use informedness and
        markedness.

        References
        ----------

        .. [1] `Arabie, P., Hubert, L. J., & De Soete, G. (1996). Clustering
               validation: results and implications for applied analyses (p.
               341).  World Scientific Pub Co Inc.
               <https://doi.org/10.1142/9789812832153_0010>`_

        .. [2] `Sim, J., & Wright, C. C. (2005). The kappa statistic in
               reliability studies: use, interpretation, and sample size
               requirements.  Physical therapy, 85(3), 257-268.
               <http://www.ncbi.nlm.nih.gov/pubmed/15733050>`_

        .. [3] `Warrens, M. J. (2008). On the equivalence of Cohen's kappa and
               the Hubert-Arabie adjusted Rand index. Journal of Classification,
               25(2), 177-183.
               <https://doi.org/10.1007/s00357-008-9023-7>`_

        .. [4] `Santos, J. M., & Embrechts, M. (2009). On the use of the
               adjusted rand index as a metric for evaluating supervised
               classification. In Artificial neural networks - ICANN 2009 (pp.
               175-184).  Springer Berlin Heidelberg.
               <https://doi.org/10.1007/978-3-642-04277-5_18>`_

        """
        p1, q1 = self.row_totals.values()
        p2, q2 = self.col_totals.values()
        a, c, d, b = self.to_ccw()
        n = self.grand_total
        if a == n or b == n or c == n or d == n:
            # only one cell is non-zero
            return np.nan
        elif p1 == 0 or p2 == 0 or q1 == 0 or q2 == 0:
            # one row or column is zero, another non-zero
            return 0.0
        else:
            # no more than one cell is zero
            return _div(2 * self.covar(), p1 * q2 + p2 * q1)

    def mp_corr(self):
        """Maxwell & Pilliner's chance-corrected association index

        Another covariance-based association index.
        """
        p1, q1 = self.row_totals.values()
        p2, q2 = self.col_totals.values()
        a, c, d, b = self.to_ccw()
        n = self.grand_total
        if a == n or b == n or c == n or d == n:
            # only one cell is non-zero
            return np.nan
        elif p1 == 0 or p2 == 0 or q1 == 0 or q2 == 0:
            # one row or column is zero, another non-zero
            return 0.0
        else:
            # no more than one cell is zero
            return _div(2 * self.covar(), p1 * q1 + p2 * q2)

    def matthews_corr(self):
        """Matthews Correlation Coefficient (Phi coefficient)

        MCC is directly related to the Chi-square statitstic. Its value is equal
        to the the Chi-square value normalized by the maximum value Chi-Square
        can achieve with given margins (for a 2x2 table, the maximum Chi-square
        score is equal to the grand total N) transformed to correlation space by
        taking a square root. MCC is a also a geometric mean of informedness and
        markedness (the regression coefficients of the problem and its dual).

        MCC is laso known as Phi Coefficient or as Yule's Q with correction for
        chance.
        """
        p1, q1 = self.row_totals.values()
        p2, q2 = self.col_totals.values()
        a, c, d, b = self.to_ccw()
        n = self.grand_total
        if a == n or b == n or c == n or d == n:
            # only one cell is non-zero
            return np.nan
        elif p1 == 0 or p2 == 0 or q1 == 0 or q2 == 0:
            # one row or column is zero, another non-zero
            return 0.0
        else:
            # no more than one cell is zero
            return _div(self.covar(), sqrt(p1 * q1 * p2 * q2))

    def mi_corr1(self):
        """One-sided regression coefficient based on mutual information

        Roughly equivalent to informedness
        """
        h, _, _ = self.entropy_metrics()
        return copysign(1, self.covar()) * sqrt(h)

    def mi_corr0(self):
        """One-sided regression coefficient based on mutual information

        Roughly equivalent to markedness
        """
        _, c, _ = self.entropy_metrics()
        return copysign(1, self.covar()) * sqrt(c)

    def mi_corr(self):
        """Two-sided correlation coefficient based on mutual information

        The coefficient decomposes into regression coefficients defined
        according to fixed-margin tables. The ``mi_corr1`` coefficient, for
        example, is obtained by dividing the G-score by the maximum achievable
        value on a table with fixed true class counts (which here correspond to
        row totals).  The ``mi_corr0`` is its dual, defined by dividing the
        G-score by its maximum achievable value with fixed predicted label
        counts (here represented as column totals).
        """
        _, _, rsquare = self.entropy_metrics()
        return copysign(1, self.covar()) * sqrt(rsquare)

    def yule_q(self):
        """Yule's Q (index of association)

        this index relates to the D odds ratio:

                   DOR - 1
           Q  =    ------- .
                   DOR + 1

        """
        a, c, d, b = self.to_ccw()
        return _div(self.covar(), a * d + b * c)

    def yule_y(self):
        """Yule's Y (Colligation Coefficient)

        The Y metric was used to produce a new association metric by adjusting
        for entropy in [1]_.

        References
        ----------

        .. [1] `Hasenclever, D., & Scholz, M. (2013). Comparing measures of
               association in 2x2 probability tables. arXiv preprint
               arXiv:1302.6161.
               <http://arxiv.org/pdf/1302.6161v1.pdf>`_

        """
        a, c, d, b = self.to_ccw()
        ad = a * d
        bc = b * c
        return _div(sqrt(ad) - sqrt(bc),
                    sqrt(ad) + sqrt(bc))

    def covar(self):
        """Determinant of a 2x2 matrix
        """
        return self.TP * self.TN - self.FP * self.FN

    # various silly terminologies folow

    # information retrieval
    precision = PPV
    recall = TPR
    fallout = FPR
    accuracy = ACC

    # clinical diagnostics
    sensitivity = TPR
    specificity = TNR

    # sales/marketing
    hit_rate = TPR
    miss_rate = FNR

    # ecology
    sm_coeff = ACC
    phi_coeff = matthews_corr

    # cluster analysis
    rand_index = ACC
    adjusted_rand_index = kappa
    fowlkes_mallows = ochiai_coeff


def mutual_info_score(labels_true, labels_pred):
    """Memory-efficeint replacement for equivalently named Sklean function
    """
    ct = ContingencyTable.from_labels(labels_true, labels_pred)
    return ct.mutual_info_score()


def homogeneity_completeness_v_measure(labels_true, labels_pred):
    """Memory-efficient replacement for equivalently named Sklearn function
    """
    ct = ContingencyTable.from_labels(labels_true, labels_pred)
    return ct.entropy_metrics()


def adjusted_rand_score(labels_true, labels_pred):
    """Memory-efficient replacement for equivalently named Sklearn function

    Example
    -------

    In a supplement to [1], the following example is given::

    >>> classes = [1, 1, 2, 2, 2, 2, 3, 3, 3, 3]
    >>> clusters = [1, 2, 1, 2, 2, 3, 3, 3, 3, 3]
    >>> round(adjusted_rand_score(classes, clusters), 3)
    0.313

    References
    ----------

    .. [1] `Yeung, K. Y., & Ruzzo, W. L. (2001). Details of the adjusted Rand
           index and clustering algorithms, supplement to the paper "An empirical
           study on principal component analysis for clustering gene expression
           data". Bioinformatics, 17(9), 763-774.
           <http://faculty.washington.edu/kayee/pca/>`_

    """
    ct = ClusteringMetrics.from_labels(labels_true, labels_pred)
    return ct.coassoc_.kappa()


def adjusted_mutual_info_score(labels_true, labels_pred):
    """Adjusted Mutual Information between two clusterings

    Examples (from SciKit-Learn)
    ----------------------------

    Perfect labelings are both homogeneous and complete, hence have
    score 1.0::

      >>> adjusted_mutual_info_score([0, 0, 1, 1], [0, 0, 1, 1])
      1.0
      >>> adjusted_mutual_info_score([0, 0, 1, 1], [1, 1, 0, 0])
      1.0

    If classes members are completely split across different clusters,
    the assignment is totally in-complete, hence the AMI is null::

      >>> adjusted_mutual_info_score([0, 0, 0, 0], [0, 1, 2, 3])
      0.0

    References
    ----------

    .. [1] `Vinh, Epps, and Bailey, (2010). Information Theoretic Measures for
       Clusterings Comparison: Variants, Properties, Normalization and
       Correction for Chance, JMLR
       <http://jmlr.csail.mit.edu/papers/volume11/vinh10a/vinh10a.pdf>`_

    """
    # labels_true, labels_pred = check_clusterings(labels_true, labels_pred)
    classes = np.unique(labels_true)
    clusters = np.unique(labels_pred)

    # Special limit cases: no clustering since the data is not split.
    # This is a perfect match hence return 1.0.
    if (classes.shape[0] == clusters.shape[0] == 1
            or classes.shape[0] == clusters.shape[0] == 0):
        return 1.0

    # Calculate the MI for the two clusterings
    cm = ContingencyTable.from_labels(labels_true, labels_pred)
    mi = cm.mutual_info_score()
    row_totals = list(cm.iter_row_totals())
    col_totals = list(cm.iter_col_totals())
    n_samples = cm.grand_total

    # Calculate the expected value for the mutual information
    emi = expected_mutual_information(row_totals, col_totals, n_samples)

    # Calculate entropy for each labeling
    h_true, h_pred = lentropy(labels_true), lentropy(labels_pred)
    ami = (mi - emi) / (max(h_true, h_pred) - emi)
    return ami


class RocCurve(object):

    """

    Example
    -------

    >>> rc = RocCurve.from_binary([0, 0, 1, 1],
    ...                           [0.1, 0.4, 0.35, 0.8])
    >>> rc.auc_score()
    0.75
    >>> rc.max_informedness()
    0.5

    """
    def __init__(self, fprs, tprs, thresholds=None, pos_label=None,
                 sample_weight=None):
        self.fprs = fprs
        self.tprs = tprs
        self.thresholds = thresholds
        self.pos_label = pos_label
        self.sample_weight = sample_weight

    @classmethod
    def from_binary(cls, y_true, y_score, sample_weight=None):
        """Convenience constructor which assumes 1 to be the positive label
        """
        fprs, tprs, thresholds = roc_curve(
            y_true, y_score, pos_label=True, sample_weight=sample_weight)
        return cls(fprs, tprs, thresholds=thresholds, sample_weight=sample_weight)

    def auc_score(self):
        """Replacement for Scikit-Learn's method

        If number of Y classes is other than two, a warning will be triggered
        but no exception thrown (the return value will be a NaN).  Also, we
        don't reorder arrays during ROC calculation since they are assumed to be
        in order.
        """
        return auc(self.fprs, self.tprs, reorder=False)

    def optimal_cutoff(self, method):
        """Calculate optimal cutoff point according to a method lambda
        """
        max_index = np.NINF
        opt_pair = (np.nan, np.nan)
        for pair in izip(self.fprs, self.tprs):
            index = method(*pair)
            if index > max_index:
                opt_pair = pair
                max_index = index
        return opt_pair, max_index

    @staticmethod
    def _informedness(fpr, tpr):
        return tpr - fpr

    def max_informedness(self):
        """Calculates maximum value of Informedness (Youden's Index)
        https://en.wikipedia.org/wiki/Youden%27s_J_statistic
        """
        return self.optimal_cutoff(self._informedness)[1]


def roc_auc_score(y_true, y_score, sample_weight=None):
    """Replaces Scikit Learn implementation (for binary y_true vectors only)
    """
    return RocCurve.from_binary(y_true, y_score).auc_score()


def matthews_corr(*args, **kwargs):
    """Return MCC score for a 2x2 contingency table
    """
    return ConfusionMatrix2.from_ccw(*args, **kwargs).matthews_corr()


def cohen_kappa(*args, **kwargs):
    """Return Cohen's Kappa for a 2x2 contingency table
    """
    return ConfusionMatrix2.from_ccw(*args, **kwargs).kappa()


def _plot_lift(xs, ys):  # pragma: no cover
    """Shortcut to plot a lift chart (for clustering_aul_score debugging)
    """
    from matplotlib import pyplot
    pyplot.plot(xs, ys, marker="o", linestyle='-')
    pyplot.xlim(xmin=0.0, xmax=1.0)
    pyplot.ylim(ymin=0.0, ymax=1.0)
    pyplot.show()


def clustering_aul_score(clusters, is_pos):
    """Area under Lift Curve (AUL) for 2 classes and n clusters

    Assume that we have a large data set of mostly unique samples where some
    underlying binary property is correlated to similarity of those samples to
    one another (i.e. whether the samples can form clusters). Assume also that
    we are not interested in whether the clusters were assigned correctly, only
    in whether our clustering is able to gather as many positive samples as
    possible, preferably (but not necessarily) in very few homogeneous clusters.
    In other words, we want our clusters to contain samples that belong to the
    positive class, while the unclustered samples (or those found in smaller
    clusters) should be negative.

    After clustering is performed, we order the clusters from the largest one to
    the smallest one, and plot a cumulative step function where the width of the
    bin under the step is proportional to cluster size, and the height of the
    bin is proportional to the cumulative number of positive samples seen so
    far. A perfect clustering (only one cluster covers the entire set of
    positives in the sample set) will have the AUL of 1.0. A failure to cluster
    or a clustering that is completely unrelated to the attribute studied will
    have AUL of 0.5.

    The application that inspired this metric was mining for positive spam
    examples in large sets of short user-generated content. Spammy content tends
    to form clusters either because creative rewriting of every single
    individual spam message is too expensive to employ in a spam campaign, or
    because, even if human or algorithmic rewriting is applied, one can still
    find features that link individual spam messages to one another because they
    all promote the same product or service or originate from the same
    geographic area.

    The AUL score calculated is very similar to the Gini index of inequality
    (area between equality and the Lorenz curve) except we do not subtract 0.5.
    """

    # TODO: don't use is_pos() function and instead provide the same interface
    # as Scikit-Learn functions that take (labels_true, labels_pred) tuple

    sortable = [(len(cluster), sum(is_pos(point) for point in cluster)) for cluster in clusters]
    # sort just by cluster size
    data = sorted(sortable, key=itemgetter(0), reverse=True)
    data = list(aggregate_tuples(data))

    total_pos = 0
    max_horizontal = 0
    max_vertical = 0

    # in the first pass, calculate some totals
    for cluster_size, pos_counts in data:
        num_clusters = len(pos_counts)
        total_pos += sum(pos_counts)
        total_in_group = cluster_size * num_clusters
        max_horizontal += total_in_group

        if cluster_size > 1:
            max_vertical += total_in_group
        else:
            max_vertical += sum(pos_counts)

    assert max_horizontal >= total_pos

    if max_vertical == 0:
        return np.nan

    aul_score = 0.0
    bin_height = 0.0
    bin_right_edge = 0

    # xs = []
    # ys = []

    # in the second pass, calculate the AUL metric:
    # for each group of clusters of the same size...
    for cluster_size, pos_counts in data:
        avg_pos_count = sum(pos_counts) / float(len(pos_counts))

        for _ in pos_counts:

            # xs.append(bin_right_edge / float(max_horizontal))

            bin_width = cluster_size
            bin_height += avg_pos_count
            bin_right_edge += bin_width
            aul_score += bin_height * bin_width

            # ys.append(bin_height / float(max_vertical))
            # xs.append(bin_right_edge / float(max_horizontal))
            # ys.append(bin_height / float(max_vertical))

    assert max_horizontal == bin_right_edge
    aul_score /= (max_vertical * max_horizontal)
    return aul_score
